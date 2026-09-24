# Copyright (c) 2026 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import pytest

from ansible import constants as C
from ansible._internal._inventory import _adjudication, _mergeplan, _provenance as prov
from ansible._internal._inventory._adjudication import InventoryMergeConflictError
from ansible.inventory.data import InventoryData
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader


@pytest.fixture
def conflicting_sources(tmp_path):
    a = tmp_path / 'a.yml'
    b = tmp_path / 'b.yml'
    c = tmp_path / 'c.yml'
    a.write_text(
        "all:\n  hosts:\n    h1:\n      x: 1\n      shared: from_a\n      mapping:\n        a: 1\n",
        encoding='utf-8')
    b.write_text(
        "all:\n  hosts:\n    h1:\n      x: 2\n      shared: from_a\n      mapping:\n        b: 2\n",
        encoding='utf-8')
    c.write_text("all:\n  hosts:\n    h2:\n      z: 3\n", encoding='utf-8')
    return a, b, c


def plan_for(*, sources, default_strategy=None, per_source=None):
    entries = []
    per_source = per_source or {}
    for path in sources:
        entry = {'path': str(path)}
        if path in per_source:
            entry['strategy'] = per_source[path]
        entries.append(entry)
    plan = {'sources': entries}
    if default_strategy:
        plan['default_strategy'] = default_strategy
    return plan


def test_parse_order_declared_then_undeclared(conflicting_sources):
    a, b, c = conflicting_sources
    manager = InventoryManager(
        DataLoader(),
        sources=[str(a), str(c), str(b)],
        merge_plan=plan_for(sources=[b, a]),
    )
    tracker = manager.merge_trace

    statuses = [s for s in tracker.source_status.values() if s.kind != 'directory']
    assert [s.source for s in statuses] == [str(b), str(a), str(c)]
    assert set(manager.hosts) == {'h1', 'h2'}


def test_last_wins_scalar_and_dict_replace(conflicting_sources):
    a, b, c = conflicting_sources
    manager = InventoryManager(
        DataLoader(), sources=[str(a), str(b)], merge_plan=plan_for(sources=[a, b]),
    )
    host = manager.get_host('h1')
    assert host.vars['x'] == 2
    # default hash_behaviour=replace: the later dict wholesale replaces the earlier one (the existing rule)
    assert host.vars['mapping'] == {'b': 2}

    proposals = [
        p for p in tracker_proposals(manager)
        if p.name == 'x' and p.entity == 'h1' and not p.internal
    ]
    by_source = {p.source: p for p in proposals}
    assert by_source[str(b)].effective is True
    assert by_source[str(a)].effective is False
    assert by_source[str(a)].reason == 'overridden'

    assert manager.merge_trace.adjudication.overrides[0].variable == 'x'


def test_last_wins_dict_merge_when_hash_behaviour_merge(conflicting_sources, monkeypatch):
    a, b, c = conflicting_sources
    monkeypatch.setattr(C, 'DEFAULT_HASH_BEHAVIOUR', 'merge')
    manager = InventoryManager(
        DataLoader(), sources=[str(a), str(b)], merge_plan=plan_for(sources=[a, b]),
    )
    # with explicit hash_behaviour=merge, combine_vars deep-merges dicts across sources as before
    assert manager.get_host('h1').vars['mapping'] == {'a': 1, 'b': 2}


def test_first_wins_keeps_earliest_value(conflicting_sources):
    a, b, c = conflicting_sources
    manager = InventoryManager(
        DataLoader(),
        sources=[str(a), str(b)],
        merge_plan=plan_for(sources=[a, b], default_strategy='first_wins'),
    )
    host = manager.get_host('h1')
    assert host.vars['x'] == 1
    assert host.vars['mapping'] == {'a': 1}  # later wholesale definition ignored, not merged

    by_source = {p.source: p for p in tracker_proposals(manager)
                 if p.name == 'x' and p.entity == 'h1' and not p.internal}
    assert by_source[str(a)].effective is True
    assert by_source[str(b)].effective is False
    assert by_source[str(b)].reason == 'overridden'


def test_first_wins_per_source_override(conflicting_sources):
    a, b, c = conflicting_sources
    manager = InventoryManager(
        DataLoader(),
        sources=[str(a), str(b)],
        merge_plan=plan_for(sources=[a, b], per_source={b: 'first_wins'}),
    )
    assert manager.get_host('h1').vars['x'] == 1


def test_equal_values_do_not_conflict_and_are_shadowed(conflicting_sources):
    a, b, c = conflicting_sources
    manager = InventoryManager(
        DataLoader(),
        sources=[str(a), str(b)],
        merge_plan=plan_for(sources=[a, b], default_strategy='error'),
    )
    assert manager.get_host('h1').vars['shared'] == 'from_a'
    by_source = {p.source: p for p in tracker_proposals(manager)
                 if p.name == 'shared' and p.entity == 'h1' and not p.internal}
    assert by_source[str(a)].effective is True
    assert by_source[str(b)].effective is False
    assert by_source[str(b)].reason == 'shadowed_by_equal_value'
    assert manager.merge_trace.adjudication.conflicts == []


def test_error_strategy_aggregates_after_all_sources_parsed(conflicting_sources, monkeypatch):
    a, b, c = conflicting_sources

    # no reconcile / vars-plugin layering may run when conflicts abort the merge
    monkeypatch.setattr(InventoryData, 'reconcile_inventory',
                        side_effect=AssertionError('reconcile must not run after merge conflict'))

    with pytest.raises(InventoryMergeConflictError) as exc_info:
        InventoryManager(
            DataLoader(),
            sources=[str(a), str(b)],
            merge_plan=plan_for(sources=[a, b], default_strategy='error'),
        )

    error = exc_info.value
    assert "variable 'x'" in str(error)
    assert str(a) in str(error) and str(b) in str(error)
    conflict = error.result.conflicts[0]
    assert conflict.entity == 'h1' and conflict.variable == 'x'
    entry_sources = {p.source for p in conflict.entries}
    assert entry_sources == {str(a), str(b)}
    # all sources were fully parsed before the aggregated error was raised
    assert {p.source for p in conflict.entries} == {str(a), str(b)}
    assert all(p.plugin for p in conflict.entries)


def test_type_difference_is_a_conflict(tmp_path):
    a = tmp_path / 'a.yml'
    b = tmp_path / 'b.yml'
    a.write_text("all:\n  hosts:\n    h1:\n      port: 1\n", encoding='utf-8')
    b.write_text("all:\n  hosts:\n    h1:\n      port: '1'\n", encoding='utf-8')

    with pytest.raises(InventoryMergeConflictError):
        InventoryManager(
            DataLoader(),
            sources=[str(a), str(b)],
            merge_plan=plan_for(sources=[a, b], default_strategy='error'),
        )


def test_synthetic_collapse_last_write_within_source(monkeypatch):
    ordered = _mergeplan.default_order(['s1']).ordered_sources
    tracker = prov.MergeProvenance(ordered)

    with tracker.source_frame('s1'):
        tracker.record_variable(prov.EntityType.HOST, 'h1', 'x', {'k': 1})
        tracker.record_variable(prov.EntityType.HOST, 'h1', 'x', {'k2': 2})

    data = InventoryData()
    data._merge_tracker = tracker
    data.add_host('h1')
    result = _adjudication.adjudicate(tracker, data)

    assert result.overrides == [] and result.conflicts == []
    # default hash_behaviour=replace: the last in-source write replaces the earlier dict wholesale
    assert data.hosts['h1'].vars['x'] == {'k2': 2}

    # under hash_behaviour=merge, repeated in-source dict writes merge exactly as Host.set_variable does
    monkeypatch.setattr(C, 'DEFAULT_HASH_BEHAVIOUR', 'merge')
    tracker2 = prov.MergeProvenance(ordered)
    with tracker2.source_frame('s1'):
        tracker2.record_variable(prov.EntityType.HOST, 'h1', 'x', {'k': 1})
        tracker2.record_variable(prov.EntityType.HOST, 'h1', 'x', {'k2': 2})
    data2 = InventoryData()
    data2._merge_tracker = tracker2
    data2.add_host('h1')
    _adjudication.adjudicate(tracker2, data2)
    assert data2.hosts['h1'].vars['x'] == {'k': 1, 'k2': 2}


def tracker_proposals(manager):
    return manager.merge_trace.proposals
