# -*- coding: utf-8 -*-
# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

from unittest.mock import patch

import pytest

from ansible import constants as C
from ansible.inventory.manager import InventoryManager
from ansible.plugins.cache.memory import CacheModule as MemoryCache
from ansible.plugins.loader import cache_loader
from ansible.utils.display import Display
from ansible.vars import fact_subsets as fs
from ansible.vars.manager import VariableManager

from units.mock.loader import DictDataLoader


@pytest.fixture
def vm():
    loader = DictDataLoader({})
    inventory = InventoryManager(loader=loader)
    direct_cache = MemoryCache()
    with patch.object(cache_loader, 'get', return_value=direct_cache):
        manager = VariableManager(loader=loader, inventory=inventory)
    inventory._inventory.add_host('h1')
    return manager


def _seed_record(vm, host, subsets_facts, ttl_map, now=1000.0, gathered=True):
    record = {}
    for subset, facts in subsets_facts.items():
        record = fs.apply_gather(
            record, facts, {subset: list(facts)}, batch_id='batch-%s' % subset,
            entry=fs.ENTRY_IMPLICIT, source='ansible.legacy.setup', now=now)
    if gathered:
        record['_ansible_facts_gathered'] = True
    vm.save_host_fact_record(host, record)
    vm._fact_subset_policy = (dict(ttl_map), fs.POLICY_FAIL)
    return record


def test_injection_strips_metadata(vm):
    _seed_record(vm, 'h1', {'hardware': {'ansible_mounts': []}}, {'hardware': 1000})

    # skip vars plugin discovery (DOCUMENTATION loading is POSIX-path specific on this test host)
    with patch('ansible.vars.manager.get_vars_from_inventory_sources', return_value={}), \
            patch('ansible.vars.manager.get_vars_from_path', return_value={}):
        variables = vm.get_vars(host=vm._inventory.get_host('h1'), include_hostvars=False)

    assert fs.META_KEY not in variables['ansible_facts']
    assert variables['ansible_facts']['mounts'] == []
    assert variables['ansible_mounts'] == []  # injected top level as well
    # metadata itself is untouched in the cache
    assert fs.META_KEY in vm.get_host_fact_record('h1')


def test_facts_fresh_for_host_scenarios(vm):
    vm._fact_subset_policy = ({'hardware': 1000, 'network': 10}, fs.POLICY_FAIL)
    assert vm.facts_fresh_for_host('h1') is False  # no record

    _seed_record(vm, 'h1',
                 {'hardware': {'ansible_devices': {}}, 'network': {'ansible_eth0': {}}},
                 {'hardware': 1000, 'network': 10}, now=1000.0)

    # within both TTLs everything is fresh
    with patch('ansible.vars.fact_subsets.time.time', return_value=1005.0):
        assert vm.facts_fresh_for_host('h1') is True

    # only the network TTL elapsed: host record is not considered fully fresh
    with patch('ansible.vars.fact_subsets.time.time', return_value=2000.0):
        assert vm.facts_fresh_for_host('h1') is False


def test_whole_record_timeout_is_a_backstop(vm):
    _seed_record(vm, 'h1', {'hardware': {'ansible_devices': {}}}, {'hardware': 1000})
    vm._fact_cache.has_expired = lambda hostname: True
    with patch('ansible.vars.fact_subsets.time.time', return_value=1005.0):
        assert vm.facts_fresh_for_host('h1') is False

    vm._fact_cache.has_expired = lambda hostname: False
    with patch('ansible.vars.fact_subsets.time.time', return_value=1005.0):
        assert vm.facts_fresh_for_host('h1') is True


def test_explicit_setup_result_gets_subset_provenance(vm):
    vm._fact_subset_policy = ({'hardware': 1000}, fs.POLICY_FAIL)

    def fake_attribution(collector_facts, terms, system=None, all_collector_classes=None):
        return {'hardware': ['ansible_mounts', 'ansible_devices', 'ansible_system']}, []

    with patch.object(fs, 'attribute_collector_facts', side_effect=fake_attribution):
        vm.set_host_facts('h1', {
            'ansible_mounts': [],
            'ansible_devices': {},
            'ansible_system': 'Linux',
            fs.COLLECTOR_FACTS_KEY: [
                {'collector': 'hardware', 'keys': ['ansible_mounts', 'ansible_devices', 'ansible_system']},
            ],
        })
    record = vm.get_host_fact_record('h1')
    assert fs.COLLECTOR_FACTS_KEY not in record
    meta = fs.get_meta(record)
    assert meta is not None
    assert meta['subsets']['hardware']['entry'] == fs.ENTRY_EXPLICIT
    assert meta['subsets']['hardware']['source'] == 'ansible.builtin.setup'
    assert meta['fact_sources']['ansible_mounts'] == 'hardware'


def test_legacy_record_forces_gather_and_status_unknown(vm):
    vm._fact_cache.set('h1', {'ansible_x': 1, '_ansible_facts_gathered': True})
    vm._fact_subset_policy = ({'hardware': 1000}, fs.POLICY_FAIL)

    assert vm.facts_fresh_for_host('h1') is False

    status = vm.get_fact_cache_status('h1', fact='ansible_x')
    assert status['provenance'] == fs.PROVENANCE_LEGACY
    assert status['facts']['ansible_x']['state'] == fs.STATE_UNKNOWN


def test_subset_and_host_invalidation(vm):
    _seed_record(vm, 'h1',
                 {'hardware': {'ansible_devices': {}}, 'network': {'ansible_eth0': {}}},
                 {'hardware': 1000, 'network': 1000})

    vm.invalidate_facts('h1', 'network')
    record = vm.get_host_fact_record('h1')
    assert 'ansible_eth0' not in record
    assert 'ansible_devices' in record
    assert 'network' not in fs.get_meta(record)['subsets']

    vm.invalidate_facts('h1')
    assert 'h1' not in vm._fact_cache._cache


def test_set_host_facts_preserves_meta_and_strips_internal_keys(vm):
    _seed_record(vm, 'h1', {'hardware': {'ansible_devices': {}}}, {'hardware': 1000})
    vm.set_host_facts('h1', {
        'ansible_new': True,
        fs.META_KEY: {'spoofed': True},
        fs.COLLECTOR_FACTS_KEY: [{'collector': 'x'}],
    })
    record = vm.get_host_fact_record('h1')
    assert record['ansible_new'] is True
    assert fs.get_meta(record)['subsets']['hardware']['source'] == 'ansible.legacy.setup'
    assert fs.COLLECTOR_FACTS_KEY not in record


def test_incapable_plugin_falls_back_with_one_warning(vm):
    class IncapableCache(MemoryCache):
        _supports_fact_subsets = False

    vm._fact_cache = IncapableCache()
    vm._fact_subset_policy = None

    real_get = C.config.get_config_value

    def fake_get(key, variables=None):
        if key == 'FACT_CACHE_SUBSET_TTL':
            return {'hardware': 100}
        if key == 'FACT_CACHE_UNAVAILABLE_POLICY':
            return 'fail'
        return real_get(key, variables=variables)

    with patch.object(C.config, 'get_config_value', side_effect=fake_get), \
            patch.object(Display, 'warning') as mock_warning:
        assert vm.fact_subsets_active() is False
        assert mock_warning.call_count == 1
        assert 'subset' in str(mock_warning.call_args)
        # memoized: no second warning
        assert vm.fact_subsets_active() is False
        assert mock_warning.call_count == 1
