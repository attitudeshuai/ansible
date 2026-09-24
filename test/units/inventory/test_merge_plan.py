# Copyright (c) 2026 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import os

from unittest import mock

import pytest

from ansible._internal._inventory import _mergeplan
from ansible._internal._inventory._mergeplan import MergeStrategy, load_merge_plan
from ansible.errors import AnsibleError, AnsibleOptionsError
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader


@pytest.fixture
def sources(tmp_path):
    return [str(tmp_path / 'a.ini'), 'host1,host2', str(tmp_path / 'b.ini'), str(tmp_path / 'c.ini')]


def valid_plan(tmp_path, **overrides):
    plan = {
        'version': 1,
        'default_strategy': 'last_wins',
        'sources': [
            {'path': str(tmp_path / 'b.ini'), 'name': 'bee', 'strategy': 'first_wins'},
            {'path': str(tmp_path / 'a.ini')},
        ],
    }
    plan.update(overrides)
    return plan


def test_resolve_order_declared_first_then_undeclared_cli_order(tmp_path, sources):
    resolved = load_merge_plan(valid_plan(tmp_path), sources=sources)

    def label(key):
        return os.path.basename(key) if os.path.isabs(key) else key

    ordered = [label(o.key) for o in resolved.ordered_sources]
    # declared sources in plan order, undeclared sources keep their relative CLI order at the end
    assert ordered == ['b.ini', 'a.ini', 'host1,host2', 'c.ini']

    by_name = {o.name: o for o in resolved.ordered_sources}
    assert by_name['bee'].strategy is MergeStrategy.FIRST_WINS
    assert by_name['bee'].declared is True
    assert by_name[str(tmp_path / 'a.ini')].strategy is MergeStrategy.LAST_WINS  # plan default
    assert by_name[str(tmp_path / 'c.ini')].strategy is MergeStrategy.LAST_WINS  # undeclared
    assert by_name[str(tmp_path / 'c.ini')].declared is False
    assert [o.order for o in resolved.ordered_sources] == [0, 1, 2, 3]


def test_resolve_order_default_strategy_and_implicit_version(tmp_path):
    sources = [str(tmp_path / name) for name in ('a.ini', 'b.ini')]
    resolved = load_merge_plan({'sources': [{'path': str(tmp_path / 'b.ini')}]}, sources=sources)

    assert resolved.plan.version == 1
    assert resolved.plan.default_strategy is MergeStrategy.LAST_WINS
    assert resolved.ordered_sources[0].key == str(tmp_path / 'b.ini')


def test_load_plan_from_yaml_file(tmp_path, sources):
    plan_file = tmp_path / 'plan.yml'
    plan_file.write_text(
        "version: 1\ndefault_strategy: error\nsources:\n"
        f"  - path: {tmp_path / 'b.ini'}\n",
        encoding='utf-8',
    )

    resolved = load_merge_plan(str(plan_file), sources=sources, loader=DataLoader())
    assert resolved.plan.default_strategy is MergeStrategy.ERROR
    assert resolved.ordered_sources[0].name == str(tmp_path / 'b.ini')


def test_load_plan_from_missing_file(tmp_path):
    with pytest.raises(AnsibleError):
        load_merge_plan(str(tmp_path / 'nope.yml'), sources=[], loader=DataLoader())


def test_load_plan_from_empty_file(tmp_path):
    plan_file = tmp_path / 'plan.yml'
    plan_file.write_text('', encoding='utf-8')

    with pytest.raises(AnsibleOptionsError, match='empty'):
        load_merge_plan(str(plan_file), sources=[], loader=DataLoader())


def test_relative_path_matches_normalized_sources(tmp_path):
    # sources and plan entries are normalized via unfrackpath before comparison
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        (tmp_path / 'rel.ini').write_text('[g]\nh\n', encoding='utf-8')
        resolved = load_merge_plan({'sources': [{'path': 'rel.ini'}]}, sources=['rel.ini'])
        assert resolved.ordered_sources[0].key == _mergeplan.source_key('rel.ini')
    finally:
        os.chdir(cwd)


@pytest.mark.parametrize('plan, match', (
    ({'sources': [{'path': '/no/such/missing.ini'}]}, 'does not match any inventory source'),
    # duplicate entries (same strategy and differing strategy are both rejected outright)
    ({'sources': [{'path': 'A'}, {'path': 'A'}]}, 'declared more than once'),
    ({'sources': [{'path': 'A', 'strategy': 'first_wins'}, {'path': 'A', 'strategy': 'error'}]},
     'declared more than once'),
    ({'default_strategy': 'nope', 'sources': []}, 'invalid conflict strategy'),
    ({'sources': [{'path': 'A', 'strategy': 'bogus'}]}, 'invalid conflict strategy'),
    ({'sources': [{'name': 'x'}]}, "requires a non-empty string 'path'"),
    ({'sources': 'A'}, "'sources' must be a list"),
    ({'bogus': 1}, 'unknown top-level'),
    ({'sources': [{'path': 'A', 'bogus': 1}]}, 'unknown key'),
    ({'version': 99, 'sources': []}, "unsupported 'version'"),
    (42, 'expected a path, a parsed mapping'),
))
def test_invalid_plans_raise(plan, match):
    sources = ['A']
    with pytest.raises(AnsibleOptionsError, match=match):
        load_merge_plan(plan, sources=sources)


def test_invalid_plan_error_lists_available_sources(sources):
    with pytest.raises(AnsibleOptionsError, match='host1,host2'):
        load_merge_plan({'sources': [{'path': '/nope.ini'}]}, sources=sources)


def test_invalid_plan_raises_before_any_parsing(sources):
    # construction must fail validation before parse_sources is entered
    with pytest.raises(AnsibleOptionsError, match='does not match any inventory source'):
        with mock.patch.object(
            InventoryManager, 'parse_sources', side_effect=AssertionError('parsing must not start')
        ):
            InventoryManager(
                loader=DataLoader(),
                sources=sources,
                merge_plan={'sources': [{'path': '/nope.ini'}]},
            )


def test_invalid_plan_file_raises_before_any_parsing(tmp_path, sources):
    plan_file = tmp_path / 'plan.yml'
    plan_file.write_text("sources:\n  - /nope.ini\n", encoding='utf-8')

    with pytest.raises(AnsibleOptionsError):
        with mock.patch.object(
            InventoryManager, 'parse_sources', side_effect=AssertionError('parsing must not start')
        ):
            InventoryManager(loader=DataLoader(), sources=sources, merge_plan=str(plan_file))


def test_manager_without_plan_has_no_resolved_plan():
    manager = InventoryManager(loader=DataLoader(), sources=[], parse=False)
    assert manager._resolved_plan is None
