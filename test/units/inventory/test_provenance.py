# Copyright (c) 2026 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import pytest

from ansible import constants as C
from ansible._internal._inventory import _provenance
from ansible._internal._inventory._provenance import EntityType, ProposalKind
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import inventory_loader


INI_INVENTORY = """
[web]
web1
web2 ansible_host=10.0.0.2

[web:vars]
gvar=from_ini
"""

YAML_INVENTORY = """
all:
  children:
    app:
      hosts:
        app1:
          yvar: from_yaml
"""


@pytest.fixture
def two_sources(tmp_path):
    ini_source = tmp_path / 'a.ini'
    ini_source.write_text(INI_INVENTORY, encoding='utf-8')
    yaml_source = tmp_path / 'b.yml'
    yaml_source.write_text(YAML_INVENTORY, encoding='utf-8')
    return ini_source, yaml_source


def _proposals(tracker, kind):
    return [p for p in tracker.proposals if p.kind is kind]


def test_default_mode_has_no_tracker(two_sources):
    ini_source, yaml_source = two_sources
    manager = InventoryManager(DataLoader(), sources=[str(ini_source), str(yaml_source)])

    assert manager.merge_trace is None
    assert manager._inventory._merge_tracker is None
    # historical parse result intact
    assert set(manager.hosts) == {'web1', 'web2', 'app1'}


def test_provenance_records_hosts_groups_memberships_and_vars(two_sources):
    ini_source, yaml_source = two_sources
    manager = InventoryManager(
        DataLoader(), sources=[str(ini_source), str(yaml_source)], merge_trace=True,
    )
    tracker = manager.merge_trace

    hosts = {p.entity: p for p in _proposals(tracker, ProposalKind.HOST_DEFINED) if not p.internal}
    assert set(hosts) == {'web1', 'web2', 'app1'}
    assert hosts['web1'].source == str(ini_source)
    assert hosts['web1'].plugin in ('ansible.builtin.ini', 'ini')
    assert hosts['app1'].source == str(yaml_source)
    assert hosts['app1'].plugin in ('ansible.builtin.yaml', 'yaml')

    groups = {p.entity: p for p in _proposals(tracker, ProposalKind.GROUP_DEFINED) if not p.internal}
    assert 'web' in groups and 'app' in groups
    assert groups['web'].source == str(ini_source)
    assert groups['app'].source == str(yaml_source)

    memberships = {(p.entity, p.name) for p in _proposals(tracker, ProposalKind.MEMBERSHIP) if not p.internal}
    assert {('web1', 'web'), ('web2', 'web'), ('app1', 'app')} <= memberships

    vars_proposals = [p for p in _proposals(tracker, ProposalKind.VARIABLE) if not p.internal]
    by_var = {(p.entity_type, p.entity, p.name): p for p in vars_proposals}

    gvar = by_var[(EntityType.GROUP, 'web', 'gvar')]
    assert gvar.source == str(ini_source)
    assert gvar.plugin in ('ansible.builtin.ini', 'ini')
    assert gvar.canonical == '"from_ini"'

    yvar = by_var[(EntityType.HOST, 'app1', 'yvar')]
    assert yvar.source == str(yaml_source)
    assert yvar.plugin in ('ansible.builtin.yaml', 'yaml')
    assert yvar.canonical == '"from_yaml"'
    # location is at least the source file path, and may carry a YAML line number
    assert str(yaml_source) in yvar.location

    ansible_host = by_var[(EntityType.HOST, 'web2', 'ansible_host')]
    assert ansible_host.source == str(ini_source)


def test_internal_vars_are_marked_internal(two_sources):
    ini_source, yaml_source = two_sources
    manager = InventoryManager(
        DataLoader(), sources=[str(ini_source), str(yaml_source)], merge_trace=True,
    )
    tracker = manager.merge_trace

    internal_vars = [
        p for p in _proposals(tracker, ProposalKind.VARIABLE)
        if p.internal and p.name in ('inventory_file', 'inventory_dir')
    ]
    assert internal_vars
    for proposal in internal_vars:
        assert proposal.plugin is None
        assert proposal.source in (str(ini_source), str(yaml_source))


def test_port_recorded_as_internal(tmp_path):
    source = tmp_path / 'ports.ini'
    source.write_text('[g]\nhost1:2222\n', encoding='utf-8')
    manager = InventoryManager(DataLoader(), sources=[str(source)], merge_trace=True)

    ports = [
        p for p in manager.merge_trace.proposals
        if p.kind is ProposalKind.VARIABLE and p.name == 'ansible_port' and p.entity == 'host1'
    ]
    assert ports and ports[0].internal is True
    assert ports[0].plugin is None
    assert manager.get_host('host1').vars['ansible_port'] == 2222


THIRD_PARTY_PLUGIN = """
from ansible.plugins.inventory import BaseInventoryPlugin


class InventoryModule(BaseInventoryPlugin):
    NAME = 'thirdparty_inventory'

    def verify_file(self, path):
        return path.endswith('.tp')

    def parse(self, inventory, loader, path, cache=True):
        super().parse(inventory, loader, path, cache=cache)
        inventory.add_group('tp_group')
        inventory.add_host('tp_host', group='tp_group')
        inventory.set_variable('tp_host', 'tp_var', 'tp_value')
"""


def test_third_party_plugin_zero_modification_provenance(tmp_path, monkeypatch):
    plugin_dir = tmp_path / 'inventory_plugins'
    plugin_dir.mkdir()
    (plugin_dir / 'thirdparty_inventory.py').write_text(THIRD_PARTY_PLUGIN, encoding='utf-8')

    source = tmp_path / 'hosts.tp'
    source.write_text('not really parsed by the plugin', encoding='utf-8')

    monkeypatch.setattr(C, 'INVENTORY_ENABLED', ['thirdparty_inventory'])
    added = inventory_loader.add_directory(str(plugin_dir))
    try:
        manager = InventoryManager(DataLoader(), sources=[str(source)], merge_trace=True)
    finally:
        if added and str(plugin_dir) in inventory_loader._extra_dirs:
            inventory_loader._extra_dirs.remove(str(plugin_dir))
            inventory_loader._clear_caches()
    tracker = manager.merge_trace

    assert 'tp_host' in manager.hosts
    assert 'tp_group' in manager.groups

    host_proposal = next(
        p for p in tracker.proposals
        if p.kind is ProposalKind.HOST_DEFINED and p.entity == 'tp_host' and not p.internal
    )
    assert host_proposal.source == str(source)
    assert host_proposal.plugin == 'thirdparty_inventory'
    assert host_proposal.location == str(source)

    var_proposal = next(
        p for p in tracker.proposals
        if p.kind is ProposalKind.VARIABLE and p.entity == 'tp_host' and p.name == 'tp_var'
    )
    assert var_proposal.canonical == '"tp_value"'

    membership = next(
        p for p in tracker.proposals
        if p.kind is ProposalKind.MEMBERSHIP and p.entity == 'tp_host' and p.name == 'tp_group'
    )
    assert membership.plugin == 'thirdparty_inventory'


def test_host_list_source_kind_and_location(tmp_path):
    manager = InventoryManager(DataLoader(), sources=['hl1,hl2'], merge_trace=True)
    tracker = manager.merge_trace

    status = next(iter(tracker.source_status.values()))
    assert status.kind == 'host_list'
    assert status.status == 'parsed'
    assert status.plugin in ('ansible.builtin.advanced_host_list', 'ansible.builtin.host_list',
                             'advanced_host_list', 'host_list')

    host_proposal = next(
        p for p in tracker.proposals if p.kind is ProposalKind.HOST_DEFINED and p.entity == 'hl1'
    )
    assert 'comma-separated host list' in host_proposal.location
