# (c) 2026 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import types

import pytest

from ansible.errors import AnsibleError
from ansible.executor.async_governance import (
    AsyncGovernanceRPC,
    GovernanceConfig,
    resolve_host_config,
    validate_play_governance,
)


def _fake_play(**overrides) -> types.SimpleNamespace:
    values = dict(
        async_governance=True,
        async_max_jobs=0,
        async_job_ttl=0,
        async_overflow_policy='wait',
        async_orphan_policy='warn',
        async_count_internal=False,
    )
    values.update(overrides)

    return types.SimpleNamespace(**values)


@pytest.fixture
def tracker() -> AsyncGovernanceRPC:
    instance = AsyncGovernanceRPC.get_instance()
    instance.configure(GovernanceConfig(enabled=True, max_jobs=2))

    return instance


def _running_result(*jids, **extra) -> dict:
    return {
        'running_jobs': [{'jid': jid, 'pid': int(jid.rpartition('.')[2]), 'results_file': f'/x/{jid}'} for jid in jids],
        'finished_jobs': extra.get('finished_jobs', []),
        'orphaned_jobs': extra.get('orphaned_jobs', []),
        'reclaimed_jobs': extra.get('reclaimed_jobs', []),
        'failed_jobs': extra.get('failed_jobs', []),
        'warnings': [],
    }


def test_enforcement_empty_host_not_full(tracker: AsyncGovernanceRPC) -> None:
    verdict = AsyncGovernanceRPC.record_enforcement.__wrapped__('h1', _running_result())

    assert not verdict['async_quota_full']
    assert verdict['running_count'] == 0
    assert verdict['governance_enabled']


def test_enforcement_quota_full(tracker: AsyncGovernanceRPC) -> None:
    verdict = AsyncGovernanceRPC.record_enforcement.__wrapped__('h1', _running_result('j1.101', 'j2.102'))

    assert verdict['async_quota_full']
    assert verdict['running_count'] == 2
    assert verdict['async_max_jobs'] == 2


def test_registered_job_counts_before_remote_observation(tracker: AsyncGovernanceRPC) -> None:
    AsyncGovernanceRPC.register_job.__wrapped__('h1', 'j1.101', False)
    verdict = AsyncGovernanceRPC.record_enforcement.__wrapped__('h1', _running_result())

    # the submitted job is not yet observed remotely but still counts
    assert verdict['running_count'] == 1
    assert not verdict['async_quota_full']


def test_pending_job_dropped_when_observed_finished(tracker: AsyncGovernanceRPC) -> None:
    AsyncGovernanceRPC.register_job.__wrapped__('h1', 'j1.101', False)
    AsyncGovernanceRPC.record_enforcement.__wrapped__(
        'h1', _running_result(finished_jobs=[{'jid': 'j1.101', 'finished_at': 1.0}])
    )

    assert AsyncGovernanceRPC.get_pending_jids.__wrapped__('h1') == []


def test_pending_job_dropped_when_reclaimed(tracker: AsyncGovernanceRPC) -> None:
    AsyncGovernanceRPC.register_job.__wrapped__('h1', 'j1.101', False)
    AsyncGovernanceRPC.record_enforcement.__wrapped__(
        'h1', _running_result(reclaimed_jobs=[{'jid': 'j1.101', 'reason': 'ttl_expired', 'reclaimed_at': 2.0}])
    )

    assert AsyncGovernanceRPC.get_pending_jids.__wrapped__('h1') == []


def test_settle_removes_bookkeeping(tracker: AsyncGovernanceRPC) -> None:
    AsyncGovernanceRPC.register_job.__wrapped__('h1', 'j1.101', False)
    AsyncGovernanceRPC.record_enforcement.__wrapped__('h1', _running_result('j1.101'))
    AsyncGovernanceRPC.settle_job.__wrapped__('h1', 'j1.101')

    snapshot = AsyncGovernanceRPC.get_host_snapshot.__wrapped__('h1')
    assert snapshot['running_jobs'] == []


def test_settle_unknown_job_is_idempotent(tracker: AsyncGovernanceRPC) -> None:
    AsyncGovernanceRPC.settle_job.__wrapped__('h1', 'j9.999')


def test_internal_jobs_excluded_by_default(tracker: AsyncGovernanceRPC) -> None:
    AsyncGovernanceRPC.register_job.__wrapped__('h1', 'ji.201', True)
    verdict = AsyncGovernanceRPC.record_enforcement.__wrapped__('h1', _running_result('ji.201'))

    assert verdict['running_count'] == 0
    assert not verdict['async_quota_full']


def test_internal_jobs_counted_when_configured() -> None:
    instance = AsyncGovernanceRPC.get_instance()
    instance.configure(GovernanceConfig(enabled=True, max_jobs=1, count_internal=True))

    AsyncGovernanceRPC.register_job.__wrapped__('h1', 'ji.201', True)
    verdict = AsyncGovernanceRPC.record_enforcement.__wrapped__('h1', _running_result('ji.201'))

    assert verdict['running_count'] == 1
    assert verdict['async_quota_full']


def test_snapshot_includes_pending_unobserved_job(tracker: AsyncGovernanceRPC) -> None:
    AsyncGovernanceRPC.register_job.__wrapped__('h1', 'j1.101', False)

    snapshot = AsyncGovernanceRPC.get_host_snapshot.__wrapped__('h1')
    assert [entry['jid'] for entry in snapshot['running_jobs']] == ['j1.101']
    assert snapshot['running_jobs'][0]['pid'] == 101


def test_last_reclamation_recorded(tracker: AsyncGovernanceRPC) -> None:
    AsyncGovernanceRPC.record_enforcement.__wrapped__(
        'h1',
        _running_result(reclaimed_jobs=[{'jid': 'j1.101', 'reason': 'ttl_expired', 'reclaimed_at': 3.0}]),
    )

    snapshot = AsyncGovernanceRPC.get_host_snapshot.__wrapped__('h1')
    assert snapshot['last_reclamation']['reclaimed_count'] == 1
    assert snapshot['last_reclamation']['reclaimed_jobs'][0]['jid'] == 'j1.101'


def test_configure_resets_state(tracker: AsyncGovernanceRPC) -> None:
    AsyncGovernanceRPC.register_job.__wrapped__('h1', 'j1.101', False)
    tracker.configure(GovernanceConfig())

    assert AsyncGovernanceRPC.get_pending_jids.__wrapped__('h1') == []
    assert AsyncGovernanceRPC.get_host_snapshot.__wrapped__('h1')['last_reclamation'] is None


def test_resolve_host_config_overrides() -> None:
    config = resolve_host_config(
        _fake_play(),
        {
            'ansible_async_max_jobs': 3,
            'ansible_async_job_ttl': 60,
            'ansible_async_overflow_policy': 'reject',
            'ansible_async_orphan_policy': 'reclaim',
        },
    )

    assert config.max_jobs == 3
    assert config.job_ttl == 60
    assert config.overflow_policy == 'reject'
    assert config.orphan_policy == 'reclaim'


@pytest.mark.parametrize(
    ('name', 'value'),
    [
        ('ansible_async_max_jobs', -1),
        ('ansible_async_max_jobs', 'nope'),
        ('ansible_async_job_ttl', -5),
        ('ansible_async_overflow_policy', 'block'),
        ('ansible_async_orphan_policy', 'delete'),
    ],
)
def test_resolve_host_config_rejects_invalid(name: str, value: object) -> None:
    with pytest.raises(ValueError):
        resolve_host_config(_fake_play(), {name: value})


def test_validate_play_governance_disabled_is_noop() -> None:
    validate_play_governance(_fake_play(async_governance=False), inventory=None, variable_manager=None)


def test_validate_play_governance_bad_host_override() -> None:
    host = types.SimpleNamespace(get_name=lambda: 'badhost')
    inventory = types.SimpleNamespace(get_hosts=lambda pattern, order: [host])
    variable_manager = types.SimpleNamespace(
        get_vars=lambda play, host: {'ansible_async_max_jobs': -1}
    )

    with pytest.raises(AnsibleError, match='badhost'):
        play = _fake_play(hosts='all', order=None)
        validate_play_governance(play, inventory, variable_manager)
