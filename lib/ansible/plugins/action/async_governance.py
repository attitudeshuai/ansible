# Copyright: (c) 2026, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

from ansible.plugins.action import ActionBase
from ansible.utils.vars import merge_hash


class ActionModule(ActionBase):
    """Resolve the async job directory and run the async_governance module on the target."""

    def _get_async_dir(self) -> str:
        # async directory based on the shell option
        async_dir = self.get_shell_option('async_dir', default="~/.ansible_async")

        return self._remote_expand_user(async_dir)

    def run(self, tmp=None, task_vars=None):
        results = super(ActionModule, self).run(tmp, task_vars)

        validation_result, new_module_args = self.validate_argument_spec(
            argument_spec={
                'mode': {'type': 'str', 'choices': ['enforce', 'query'], 'default': 'enforce'},
                'job_ttl': {'type': 'int', 'default': 0},
                'orphan_policy': {'type': 'str', 'choices': ['warn', 'reclaim', 'fail'], 'default': 'warn'},
                'submitted_jids': {'type': 'list', 'elements': 'str', 'default': []},
            },
        )

        mode = new_module_args['mode']
        target_host = self._task.delegate_to or task_vars['inventory_hostname']

        async_dir = self._get_async_dir()
        new_module_args['_async_dir'] = async_dir

        # tracker interactions only exist while a play strategy is active
        tracker = None
        try:
            from ansible._internal._plugins import _strategy
            _strategy.StrategyContext.current()
        except Exception:
            pass
        else:
            from ansible.executor.async_governance import AsyncGovernanceRPC
            tracker = AsyncGovernanceRPC.get_client()

        if tracker is not None and mode == 'enforce':
            # include jobs submitted during this run that the remote scan may not observe yet
            new_module_args['submitted_jids'] = sorted(
                set(new_module_args['submitted_jids']) | set(tracker.get_pending_jids(target_host))
            )

        module_result = self._execute_module(
            module_name='ansible.legacy.async_governance',
            task_vars=task_vars,
            module_args=new_module_args,
        )
        results = merge_hash(results, module_result)

        if tracker is not None:
            if mode == 'enforce':
                verdict = tracker.record_enforcement(target_host, results)
                results.update(verdict)
            else:
                self._merge_tracker_snapshot(results, tracker.get_host_snapshot(target_host))

        results.setdefault('governance_enabled', False)
        results.setdefault('last_reclamation', None)
        results.setdefault('running_count', len(results.get('running_jobs', [])))

        return results

    @staticmethod
    def _merge_tracker_snapshot(results: dict, snapshot: dict) -> None:
        """Merge the controller-side tracker snapshot into a read-only query result."""
        remote_running = {entry['jid']: entry for entry in results.get('running_jobs', [])}

        for entry in snapshot.get('running_jobs', []):
            jid = entry['jid']
            if jid not in remote_running:
                # submitted this run but not observed by the remote scan yet
                entry = dict(entry, pending=True)
                remote_running[jid] = entry

        merged_running = sorted(remote_running.values(), key=lambda entry: entry['jid'])

        remote_orphans = {entry['jid']: entry for entry in results.get('orphaned_jobs', [])}
        for entry in snapshot.get('orphaned_jobs', []):
            remote_orphans.setdefault(entry['jid'], entry)

        results['running_jobs'] = merged_running
        results['running_count'] = len(merged_running)
        results['orphaned_jobs'] = sorted(remote_orphans.values(), key=lambda entry: entry['jid'])
        results['last_reclamation'] = snapshot.get('last_reclamation')
        results['governance_enabled'] = snapshot.get('governance_enabled', False)
