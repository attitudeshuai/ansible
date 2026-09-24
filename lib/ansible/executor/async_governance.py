# (c) 2026 Ansible Project
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

"""Async job lifecycle governance.

Holds the controller-side tracker for async jobs: per-host running sets, abandoned jobs from
previous runs and the results of the latest reclamation pass. The tracker state is mutated
serially on the strategy result thread, so the methods below do not require locking.
"""

from __future__ import annotations

import dataclasses
import time
import typing as t

from ansible.errors import AnsibleError
from ansible._internal._rpc_host import AutoRegisterRPC
from ansible._internal._worker._inventory_rpc import dispatch_to_strategy_result_thread

if t.TYPE_CHECKING:
    from ansible.inventory.host import Host
    from ansible.playbook.play import Play
    from ansible.vars.manager import VariableManager

__all__ = ['AsyncGovernanceRPC', 'GovernanceConfig', 'resolve_host_config', 'validate_play_governance']

_OVERFLOW_POLICIES = frozenset(('wait', 'reject'))
_ORPHAN_POLICIES = frozenset(('warn', 'reclaim', 'fail'))

# host variable overrides for the play-level keywords
_HOST_MAX_JOBS = 'ansible_async_max_jobs'
_HOST_JOB_TTL = 'ansible_async_job_ttl'
_HOST_OVERFLOW_POLICY = 'ansible_async_overflow_policy'
_HOST_ORPHAN_POLICY = 'ansible_async_orphan_policy'


@dataclasses.dataclass(frozen=True, kw_only=True)
class GovernanceConfig:
    """Resolved governance configuration for a play."""

    enabled: bool = False
    max_jobs: int = 0
    job_ttl: int = 0
    overflow_policy: str = 'wait'
    orphan_policy: str = 'warn'
    count_internal: bool = False

    @classmethod
    def from_play(cls, play: Play) -> GovernanceConfig:
        return cls(
            enabled=bool(play.async_governance),
            max_jobs=play.async_max_jobs,
            job_ttl=play.async_job_ttl,
            overflow_policy=play.async_overflow_policy,
            orphan_policy=play.async_orphan_policy,
            count_internal=bool(play.async_count_internal),
        )


@dataclasses.dataclass(kw_only=True)
class _HostState:
    """Governance state tracked for a single target host."""

    observed_running: dict[str, dict] = dataclasses.field(default_factory=dict)
    """Jobs reported running by the latest remote enforcement pass, keyed by job id."""

    pending: set[str] = dataclasses.field(default_factory=set)
    """Jobs submitted during the current run not yet observed as finished remotely."""

    internal: set[str] = dataclasses.field(default_factory=set)
    """Jobs launched internally by the framework."""

    orphaned: dict[str, dict] = dataclasses.field(default_factory=dict)
    """Jobs abandoned by a previous run, keyed by job id."""

    last_reclamation: dict | None = None
    """Result of the latest reclamation pass."""


def _coerce_non_negative_int(name: str, value: object) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer, not a boolean")

    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a non-negative integer") from None

    if result < 0:
        raise ValueError(f"{name} must be a non-negative integer")

    return result


def _coerce_choice(name: str, value: object, choices: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        formatted = ', '.join(sorted(choices))
        raise ValueError(f"{name} must be one of: {formatted}")

    return value


def resolve_host_config(play: Play, task_vars: dict[str, object]) -> GovernanceConfig:
    """Resolve the governance config for a host, applying host variable overrides."""
    config = GovernanceConfig.from_play(play)

    max_jobs = task_vars.get(_HOST_MAX_JOBS, config.max_jobs)
    job_ttl = task_vars.get(_HOST_JOB_TTL, config.job_ttl)
    overflow_policy = task_vars.get(_HOST_OVERFLOW_POLICY, config.overflow_policy)
    orphan_policy = task_vars.get(_HOST_ORPHAN_POLICY, config.orphan_policy)

    return dataclasses.replace(
        config,
        max_jobs=_coerce_non_negative_int(_HOST_MAX_JOBS, max_jobs),
        job_ttl=_coerce_non_negative_int(_HOST_JOB_TTL, job_ttl),
        overflow_policy=_coerce_choice(_HOST_OVERFLOW_POLICY, overflow_policy, _OVERFLOW_POLICIES),
        orphan_policy=_coerce_choice(_HOST_ORPHAN_POLICY, orphan_policy, _ORPHAN_POLICIES),
    )


def validate_play_governance(play: Play, inventory, variable_manager: VariableManager) -> None:
    """Validate play and host governance settings before a run starts."""
    if not play.async_governance:
        return

    play_hosts = inventory.get_hosts(play.hosts, order=play.order)

    for host in play_hosts:
        host_vars = variable_manager.get_vars(play=play, host=host)

        try:
            resolve_host_config(play, host_vars)
        except ValueError as ex:
            raise AnsibleError(f"Invalid async governance setting for host {host.get_name()!r}: {ex}") from None


class AsyncGovernanceRPC(AutoRegisterRPC):
    """Worker-facing tracker for async job governance.

    Methods execute serially on the strategy result thread. The single instance is created at
    class definition time and is shared across plays, so ``configure`` resets all state per play.
    """

    def __init__(self) -> None:
        self._config = GovernanceConfig()
        self._hosts: dict[str, _HostState] = {}

    @classmethod
    def get_instance(cls) -> t.Self:
        """Return the single server-side instance, for use in the controller process."""
        return t.cast(t.Self, cls._instance)

    def configure(self, config: GovernanceConfig) -> None:
        """Reset tracker state for a new play."""
        self._config = config
        self._hosts = {}

    def _state(self, host: str) -> _HostState:
        return self._hosts.setdefault(host, _HostState())

    @staticmethod
    @dispatch_to_strategy_result_thread
    def get_pending_jids(host: str) -> list[str]:
        """Return the job ids submitted this run not yet observed as finished."""
        self = AsyncGovernanceRPC.get_instance()

        return sorted(self._state(host).pending)

    @staticmethod
    @dispatch_to_strategy_result_thread
    def register_job(host: str, jid: str, internal: bool) -> None:
        """Record a successfully submitted async job."""
        self = AsyncGovernanceRPC.get_instance()
        state = self._state(host)

        state.pending.add(jid)
        if internal:
            state.internal.add(jid)

    @staticmethod
    @dispatch_to_strategy_result_thread
    def record_enforcement(host: str, remote_result: dict) -> dict:
        """Reconcile tracker state with a remote enforcement result and return the quota verdict."""
        self = AsyncGovernanceRPC.get_instance()
        config = self._config
        state = self._state(host)

        remote_running = {entry['jid']: entry for entry in remote_result.get('running_jobs', [])}
        remote_finished = {entry['jid'] for entry in remote_result.get('finished_jobs', [])}
        remote_reclaimed = {entry['jid'] for entry in remote_result.get('reclaimed_jobs', [])}
        remote_orphaned = {entry['jid']: entry for entry in remote_result.get('orphaned_jobs', [])}
        settled = remote_finished | remote_reclaimed

        state.observed_running = remote_running
        state.orphaned = remote_orphaned

        # pending jobs are settled only when explicitly observed finished or reclaimed;
        # a scan that simply does not observe them may have run while their status file was initializing
        state.pending = {jid for jid in state.pending if jid not in settled}

        state.last_reclamation = {
            'at': time.time(),
            'reclaimed_jobs': remote_result.get('reclaimed_jobs', []),
            'reclaimed_count': len(remote_result.get('reclaimed_jobs', [])),
            'warnings': remote_result.get('warnings', []),
        }

        all_running = set(remote_running) | state.pending
        external_running = all_running - state.internal

        if config.count_internal:
            counted_running = all_running
        else:
            counted_running = external_running

        quota_full = config.max_jobs > 0 and len(counted_running) >= config.max_jobs

        return {
            'governance_enabled': config.enabled,
            'running_count': len(counted_running),
            'async_max_jobs': config.max_jobs,
            'async_quota_full': quota_full,
            'async_failed_orphans': remote_result.get('failed_jobs', []),
        }

    @staticmethod
    @dispatch_to_strategy_result_thread
    def settle_job(host: str, jid: str) -> None:
        """Drop bookkeeping for a job after polling ended.

        A still-running remote job is rediscovered by the next enforcement scan.
        """
        self = AsyncGovernanceRPC.get_instance()
        state = self._state(host)

        state.pending.discard(jid)
        state.internal.discard(jid)
        state.observed_running.pop(jid, None)
        state.orphaned.pop(jid, None)

    @staticmethod
    @dispatch_to_strategy_result_thread
    def get_host_snapshot(host: str) -> dict:
        """Return a serializable snapshot used to answer a query on the host."""
        self = AsyncGovernanceRPC.get_instance()

        if host not in self._hosts:
            return {
                'governance_enabled': self._config.enabled,
                'running_jobs': [],
                'orphaned_jobs': [],
                'last_reclamation': None,
            }

        state = self._state(host)

        running_jobs: list[dict] = []
        for jid in sorted(state.observed_running):
            entry = dict(state.observed_running[jid])
            entry['internal'] = jid in state.internal
            running_jobs.append(entry)

        for jid in sorted(state.pending - set(state.observed_running)):
            running_jobs.append({
                'jid': jid,
                'pid': _pid_from_jid(jid),
                'internal': jid in state.internal,
            })

        return {
            'governance_enabled': self._config.enabled,
            'running_jobs': running_jobs,
            'orphaned_jobs': sorted(state.orphaned.values(), key=lambda entry: entry['jid']),
            'last_reclamation': state.last_reclamation,
        }


def _pid_from_jid(jid: str) -> int | None:
    """Extract the remote process id encoded in a job id."""
    try:
        return int(jid.rpartition('.')[2])
    except ValueError:
        return None
