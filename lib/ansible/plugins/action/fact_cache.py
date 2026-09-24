# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

from ansible.errors import AnsibleActionFail
from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    """Controller-side inspection and invalidation of per-subset fact cache records."""

    TRANSFERS_FILES = False
    _requires_connection = False
    _supports_check_mode = True
    _VALID_ARGS = frozenset(('state', 'host', 'subset', 'fact'))

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = {}

        result = super().run(tmp, task_vars)
        del tmp

        if self._variable_manager is None:
            raise AnsibleActionFail("The ansible.builtin.fact_cache action requires the controller variable manager, which is unavailable.")

        args = self._task.args or {}
        state = args.get('state', 'status')
        if state not in ('status', 'invalidate'):
            raise AnsibleActionFail(f"Invalid state {state!r}; valid values are 'status' and 'invalidate'.")

        host = args.get('host') or task_vars.get('inventory_hostname')
        if not host:
            raise AnsibleActionFail("No target host could be determined for fact cache management.")

        subsets = args.get('subset')
        if isinstance(subsets, str):
            subsets = [subsets]
        if subsets is not None and not all(isinstance(item, str) for item in subsets):
            raise AnsibleActionFail("'subset' must be a list of subset names.")

        fact = args.get('fact')
        if fact is not None and not isinstance(fact, str):
            raise AnsibleActionFail("'fact' must be a fact name string.")

        if self._task.check_mode:
            result['changed'] = False
            if state == 'invalidate':
                result['invalidated'] = {host: list(subsets) if subsets else None}
            else:
                result['status'] = self._variable_manager.get_fact_cache_status(host, fact=fact, subsets=subsets)
            return result

        if state == 'invalidate':
            if subsets:
                for subset in subsets:
                    self._variable_manager.invalidate_facts(host, subset)
            else:
                self._variable_manager.invalidate_facts(host, None)
            result['changed'] = True
            result['invalidated'] = {host: list(subsets) if subsets else None}
        else:
            result['changed'] = False
            result['status'] = self._variable_manager.get_fact_cache_status(host, fact=fact, subsets=subsets)

        return result
