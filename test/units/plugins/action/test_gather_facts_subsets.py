# -*- coding: utf-8 -*-
# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest

from ansible import constants as C
from ansible.errors import AnsibleConnectionFailure
from ansible.inventory.manager import InventoryManager
from ansible.playbook.task import Task
from ansible.plugins.action.gather_facts import ActionModule as GatherFactsAction
from ansible.plugins.loader import cache_loader
from ansible.plugins.cache.memory import CacheModule as MemoryCache
from ansible._internal._templating._engine import TemplateEngine
from ansible.vars import fact_subsets as fs
from ansible.vars.manager import VariableManager

from units.mock.loader import DictDataLoader


SETUP = 'ansible.legacy.setup'
PKG = 'ansible.builtin.package_facts'


class FakeVariableManager:
    def __init__(self, ttl_map, policy=fs.POLICY_FAIL, record=None):
        self._policy = (dict(ttl_map), policy)
        self.records = {'h1': record if record is not None else {}}
        self.saved = []

    whole_expired = False

    def fact_subsets_active(self):
        return True

    def whole_record_expired(self, host):
        return self.whole_expired

    def _resolve_fact_subset_policy(self):
        return self._policy

    def get_host_fact_record(self, host):
        return self.records.get(host, {})

    def save_host_fact_record(self, host, record):
        self.records[host] = record
        self.saved.append(copy.deepcopy(record))


@pytest.fixture
def plugin():
    task = MagicMock(Task)
    task.action = 'gather_facts'
    task.async_val = 0
    task.args = {}
    task.collections = None
    task._implicit_gather = True  # MagicMock bools False otherwise; these tests cover the implicit path
    connection = MagicMock()
    connection.check_mode = False
    templar = TemplateEngine(loader=DictDataLoader({}))

    action = GatherFactsAction(task, connection, MagicMock(), loader=None, templar=templar, shared_loader_obj=None)
    action._get_module_args = MagicMock(return_value={})
    action._resolved_module_fqcn = MagicMock(side_effect=lambda name: name)
    action._remove_tmp_path = MagicMock()
    return action


def _run(plugin, fake_vm, *, behavior='ok', hosts=(SETUP,), parallel=False, extra_args=None):
    plugin._variable_manager = fake_vm
    plugin._task.args = {'parallel': parallel}
    if extra_args:
        plugin._task.args.update(extra_args)
    calls = []

    def execute_module(module_name=None, module_args=None, task_vars=None, wrap_async=False):
        calls.append({'module': module_name, 'args': copy.deepcopy(module_args)})
        if behavior == 'unreachable':
            raise AnsibleConnectionFailure('host unreachable')
        if behavior == 'setup_fails' and module_name == SETUP:
            return {'failed': True, 'msg': 'boom', 'exception': object()}
        if module_name == PKG:
            return {'ansible_facts': {'ansible_facts_packages': {'vim': '1.0'}}}
        if behavior == 'filtered':
            return {'ansible_facts': {
                'ansible_mounts': '/mnt/new',
                fs.COLLECTOR_FACTS_KEY: [{'collector': 'hardware', 'keys': ['ansible_mounts']}],
            }}
        return {'ansible_facts': {
            'ansible_mounts': '/mnt/new',
            'ansible_system': 'Linux',
            fs.COLLECTOR_FACTS_KEY: [
                {'collector': 'hardware', 'keys': ['ansible_mounts', 'ansible_system']},
            ],
        }}

    plugin._execute_module = MagicMock(side_effect=execute_module)

    def fake_attribution(collector_facts, terms, system=None, all_collector_classes=None):
        return {term: ['ansible_mounts'] if term == 'mounts' else ['ansible_mounts']
                for term in terms if term == 'mounts'}, ['ansible_system']

    def resolve_smart(self, modules, task_vars):
        if 'smart' in modules:
            modules.pop(modules.index('smart'))
            modules.append(SETUP)

    task_vars = {'inventory_hostname': 'h1'}
    with patch.object(plugin.__class__, '_handle_smart', resolve_smart), \
            patch.object(fs, 'attribute_collector_facts', side_effect=fake_attribution), \
            patch('ansible.plugins.action.gather_facts.time.time', return_value=2000.0):
        result = plugin.run(task_vars=task_vars)

    return result, calls


def _full_record(now=1000.0, mounts='/mnt/old'):
    record = fs.apply_gather(
        {}, {'ansible_mounts': mounts, 'ansible_system': 'Linux'},
        {'mounts': ['ansible_mounts']},
        batch_id='old-batch', entry=fs.ENTRY_IMPLICIT, source=SETUP, now=now)
    record['_ansible_facts_gathered'] = True
    return record


def test_first_gather_is_full_and_records_provenance(plugin):
    vm = FakeVariableManager({'mounts': 100_000})
    result, calls = _run(plugin, vm)

    assert not result.get('failed', False)
    assert calls[0]['args'].get('gather_subset') is None  # play default full gather, no subset override
    saved = vm.saved[-1]
    assert saved['_ansible_facts_gathered'] is True
    assert saved[fs.META_KEY]['subsets']['mounts']['source'] == SETUP
    assert fs.COLLECTOR_FACTS_KEY not in saved
    # the internal collector channel is stripped from the returned result as well
    assert fs.COLLECTOR_FACTS_KEY not in result['ansible_facts']


def test_default_path_connection_failure_propagates(plugin):
    """With no subset policy, unreachable keeps historical semantics: the exception propagates."""
    plugin._variable_manager = None
    plugin._task.args = {}
    plugin._get_module_args = MagicMock(return_value={})
    plugin._resolved_module_fqcn = MagicMock(side_effect=lambda name: name)
    plugin._execute_module = MagicMock(side_effect=AnsibleConnectionFailure('down'))

    def resolve_smart(self, modules, task_vars):
        if 'smart' in modules:
            modules.pop(modules.index('smart'))
            modules.append(SETUP)

    with patch.object(plugin.__class__, '_handle_smart', resolve_smart):
        with pytest.raises(AnsibleConnectionFailure):
            plugin.run(task_vars={'inventory_hostname': 'h1'})


def test_undeclared_extra_module_unreachable_still_fails(plugin):
    """A non-TTL extra fact module's connection failure must not be swallowed."""
    vm = FakeVariableManager({'mounts': 100_000}, policy=fs.POLICY_FAIL, record=_full_record())

    def fake_config(key, variables=None):
        if key == 'FACTS_MODULES':
            return [SETUP, PKG]
        raise AssertionError('unexpected config key %r' % key)

    plugin._variable_manager = vm
    plugin._task.args = {'parallel': False}

    def execute_module(module_name=None, module_args=None, task_vars=None, wrap_async=False):
        if module_name == PKG:
            raise AnsibleConnectionFailure('down')
        raise AssertionError('setup should have been skipped as fresh')

    plugin._execute_module = MagicMock(side_effect=execute_module)

    with patch('ansible.plugins.action.gather_facts.C.config.get_config_value', side_effect=fake_config), \
            patch('ansible.plugins.action.gather_facts.time.time', return_value=2000.0):
        result = plugin.run(task_vars={'inventory_hostname': 'h1'})

    assert result.get('failed') is True
    assert PKG in result['failed_modules']


def test_all_fresh_skips_gather(plugin):
    vm = FakeVariableManager({'mounts': 100_000}, record=_full_record())
    result, calls = _run(plugin, vm)

    assert calls == []
    assert not result.get('failed', False)
    assert result['fact_cache']['fresh_skipped'] == [SETUP]
    assert vm.saved == []


def test_only_stale_subset_is_regathered(plugin):
    vm = FakeVariableManager({'hardware': 100_000, 'mounts': 10}, record=_full_record())
    # add a fresh hardware subset to the record
    record = fs.apply_gather(
        vm.records['h1'], {'ansible_devices': {}}, {'hardware': ['ansible_devices']},
        batch_id='hw-batch', entry=fs.ENTRY_IMPLICIT, source=SETUP, now=1000.0)
    vm.records['h1'] = record

    result, calls = _run(plugin, vm)

    assert not result.get('failed', False)
    assert calls[0]['args']['gather_subset'] == ['!all', '!min', 'mounts']
    saved = vm.saved[-1]
    meta = saved[fs.META_KEY]
    assert meta['subsets']['mounts']['batch_id'] != 'old-batch'
    assert meta['subsets']['hardware']['batch_id'] == 'hw-batch'
    assert saved['ansible_mounts'] == '/mnt/new'
    assert saved['ansible_devices'] == {}


def test_fail_policy_unreachable_fails_without_writing(plugin):
    vm = FakeVariableManager({'mounts': 10}, policy=fs.POLICY_FAIL, record=_full_record())
    result, calls = _run(plugin, vm, behavior='unreachable')

    assert result.get('failed') is True
    assert 'mounts' in result['failed_modules'][SETUP].get('msg', '')
    # old content/time kept, nothing new written
    assert vm.saved == []
    assert vm.records['h1']['ansible_mounts'] == '/mnt/old'


def test_stale_policy_serves_expired_values_with_marker(plugin):
    vm = FakeVariableManager({'mounts': 10}, policy=fs.POLICY_STALE, record=_full_record())
    result, calls = _run(plugin, vm, behavior='unreachable')

    assert not result.get('failed', False), result
    stale = result[fs.RESULT_STALE_KEY]
    assert [item['subset'] for item in stale] == ['mounts']
    assert stale[0]['expired_for'] == 990.0
    assert vm.saved == []  # no new success record


def test_stale_policy_without_old_values_still_fails(plugin):
    vm = FakeVariableManager({'mounts': 10}, policy=fs.POLICY_STALE, record={})
    result, calls = _run(plugin, vm, behavior='unreachable')
    assert result.get('failed') is True
    assert vm.saved == []


def test_whole_record_timeout_backstop_forces_full_gather(plugin):
    # even though the subset is fresh, a record older than the global timeout forces a full gather
    vm = FakeVariableManager({'mounts': 100_000}, record=_full_record())
    vm.whole_expired = True

    result, calls = _run(plugin, vm)
    assert not result.get('failed', False), result
    assert calls[0]['args'].get('gather_subset') is None  # play's full gather_subset, no subset override
    assert result['fact_cache']['fresh_skipped'] == []


def test_filtered_refresh_does_not_prune_subset_facts(plugin):
    # record owns mounts AND devices under the mounts subset; the filtered run returns only mounts
    record = fs.apply_gather(
        {}, {'ansible_mounts': '/mnt/old', 'ansible_devices': {}, 'ansible_system': 'Linux'},
        {'mounts': ['ansible_mounts', 'ansible_devices']},
        batch_id='old', entry=fs.ENTRY_IMPLICIT, source=SETUP, now=1000.0)
    record['_ansible_facts_gathered'] = True
    vm = FakeVariableManager({'mounts': 10}, policy=fs.POLICY_FAIL, record=record)

    result, calls = _run(plugin, vm, behavior='filtered', extra_args={'filter': 'ansible_mounts'})
    assert not result.get('failed', False), result
    saved = vm.saved[-1]
    assert saved['ansible_mounts'] == '/mnt/new'
    assert saved['ansible_devices'] == {}  # filtered output must not prune the other subset fact


def test_partial_failure_keeps_successful_module_and_drops_failed(plugin):
    vm = FakeVariableManager(
        {'mounts': 10, PKG: 1000}, policy=fs.POLICY_FAIL, record=_full_record())

    # two fact modules, serial
    def fake_config(key, variables=None):
        if key == 'FACTS_MODULES':
            return [SETUP, PKG]
        raise AssertionError('unexpected config key %r' % key)

    task_vars = {'inventory_hostname': 'h1'}
    plugin._variable_manager = vm
    plugin._task.args = {'parallel': False}
    calls = []

    def execute_module(module_name=None, module_args=None, task_vars=None, wrap_async=False):
        calls.append(module_name)
        if module_name == SETUP:
            raise AnsibleConnectionFailure('down')
        return {'ansible_facts': {'ansible_facts_packages': {'vim': '1.0'}}}

    plugin._execute_module = MagicMock(side_effect=execute_module)

    with patch('ansible.plugins.action.gather_facts.C.config.get_config_value', side_effect=fake_config), \
            patch('ansible.plugins.action.gather_facts.time.time', return_value=2000.0):
        result = plugin.run(task_vars=task_vars)

    assert result.get('failed') is True
    saved = vm.saved[-1]
    assert saved['ansible_facts_packages'] == {'vim': '1.0'}
    assert saved[fs.META_KEY]['subsets'][PKG]['source'] == PKG
    # the failed setup subset keeps its original batch
    assert saved[fs.META_KEY]['subsets']['mounts']['batch_id'] == 'old-batch'
    assert saved['ansible_mounts'] == '/mnt/old'


def test_end_to_end_with_real_variable_manager_stale_policy(plugin):
    """Real VariableManager + memory cache + config-driven policy through the gather action."""
    loader = DictDataLoader({})
    inventory = InventoryManager(loader=loader)
    direct_cache = MemoryCache()
    with patch.object(cache_loader, 'get', return_value=direct_cache):
        vm = VariableManager(loader=loader, inventory=inventory)

    # stale mounts record, policy stale
    record = fs.apply_gather(
        {}, {'ansible_mounts': '/mnt/old', 'ansible_system': 'Linux'}, {'mounts': ['ansible_mounts']},
        batch_id='old', entry=fs.ENTRY_IMPLICIT, source=SETUP, now=1000.0)
    record['_ansible_facts_gathered'] = True
    vm.save_host_fact_record('h1', record)

    real_get = C.config.get_config_value

    def fake_config(key, variables=None):
        if key == 'FACT_CACHE_SUBSET_TTL':
            return {'mounts': 10}
        if key == 'FACT_CACHE_UNAVAILABLE_POLICY':
            return 'stale'
        if key == 'FACTS_MODULES':
            return ['smart']
        return real_get(key, variables=variables)

    plugin._variable_manager = vm
    plugin._task.args = {'parallel': False}
    plugin._execute_module = MagicMock(side_effect=AnsibleConnectionFailure('down'))

    def resolve_smart(self, modules, task_vars):
        if 'smart' in modules:
            modules.pop(modules.index('smart'))
            modules.append(SETUP)

    def fake_attribution(collector_facts, terms, system=None, all_collector_classes=None):
        return {'mounts': ['ansible_mounts']}, ['ansible_system']

    with patch.object(C.config, 'get_config_value', side_effect=fake_config), \
            patch.object(plugin.__class__, '_handle_smart', resolve_smart), \
            patch.object(fs, 'attribute_collector_facts', side_effect=fake_attribution), \
            patch('ansible.plugins.action.gather_facts.time.time', return_value=2000.0):
        result = plugin.run(task_vars={'inventory_hostname': 'h1'})

    assert not result.get('failed', False), result
    assert result[fs.RESULT_STALE_KEY][0]['subset'] == 'mounts'

    status = vm.get_fact_cache_status('h1', fact='ansible_mounts')
    assert status['subsets'][0]['state'] == fs.STATE_STALE
    assert status['facts']['ansible_mounts']['batch_id'] == 'old'
