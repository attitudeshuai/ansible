# -*- coding: utf-8 -*-
# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ansible.errors import AnsibleActionFail
from ansible.playbook.task import Task
from ansible.plugins.action.fact_cache import ActionModule as FactCacheAction
from ansible._internal._templating._engine import TemplateEngine

from units.mock.loader import DictDataLoader


class FakeVariableManager:
    def __init__(self):
        self.invalidations = []
        self.status_calls = []

    def get_fact_cache_status(self, host, fact=None, subsets=None):
        self.status_calls.append((host, fact, subsets))
        return {'host': host, 'provenance': 'full', 'subsets': [], 'facts': {}, 'gathered': True}

    def invalidate_facts(self, host, subset=None):
        self.invalidations.append((host, subset))


@pytest.fixture
def action():
    task = MagicMock(Task)
    task.check_mode = False
    task.async_val = 0
    task.collections = None
    plugin = FactCacheAction(
        task, MagicMock(), MagicMock(), loader=None,
        templar=TemplateEngine(loader=DictDataLoader({})), shared_loader_obj=None)
    plugin._variable_manager = FakeVariableManager()
    return plugin


def test_status(action):
    action._task.args = {'state': 'status', 'subset': ['mounts'], 'fact': 'ansible_mounts'}
    result = action.run(task_vars={'inventory_hostname': 'h1'})

    assert result['changed'] is False
    assert result['status']['provenance'] == 'full'
    assert action._variable_manager.status_calls == [('h1', 'ansible_mounts', ['mounts'])]


def test_status_default_host(action):
    action._task.args = {}
    result = action.run(task_vars={'inventory_hostname': 'h1'})
    assert result['status']['host'] == 'h1'


def test_invalidate_subset_and_host(action):
    action._task.args = {'state': 'invalidate', 'subset': ['mounts', 'network']}
    result = action.run(task_vars={'inventory_hostname': 'h1'})
    assert result['changed'] is True
    assert action._variable_manager.invalidations == [('h1', 'mounts'), ('h1', 'network')]

    action._variable_manager.invalidations.clear()
    action._task.args = {'state': 'invalidate'}
    result = action.run(task_vars={'inventory_hostname': 'h1'})
    assert action._variable_manager.invalidations == [('h1', None)]


def test_invalid_state(action):
    action._task.args = {'state': 'nope'}
    with pytest.raises(AnsibleActionFail):
        action.run(task_vars={'inventory_hostname': 'h1'})


def test_check_mode_does_not_invalidate(action):
    action._task.args = {'state': 'invalidate', 'subset': ['mounts']}
    action._task.check_mode = True
    result = action.run(task_vars={'inventory_hostname': 'h1'})
    assert result['changed'] is False
    assert action._variable_manager.invalidations == []


def test_missing_variable_manager_fails_clearly():
    task = MagicMock(Task)
    task.check_mode = False
    task.async_val = 0
    plugin = FactCacheAction(task, MagicMock(), MagicMock(), loader=None,
                             templar=TemplateEngine(loader=DictDataLoader({})), shared_loader_obj=None)
    with pytest.raises(AnsibleActionFail, match='variable manager'):
        plugin.run(task_vars={'inventory_hostname': 'h1'})
