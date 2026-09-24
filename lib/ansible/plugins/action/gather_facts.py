# Copyright (c) 2017 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import os
import time
import typing as t

from ansible import constants as C
from ansible.errors import AnsibleActionFail, AnsibleConnectionFailure
from ansible.executor.module_common import _apply_action_arg_defaults
from ansible.module_utils.common.text.converters import to_native
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase
from ansible.utils.vars import merge_hash
from ansible._internal._errors import _error_utils
from ansible.vars import fact_subsets


class ActionModule(ActionBase):

    _supports_check_mode = True

    def _get_module_args(self, fact_module: str, task_vars: dict[str, t.Any]) -> dict[str, t.Any]:

        mod_args = self._task.args.copy()

        # deal with 'setup specific arguments'
        if fact_module not in C._ACTION_SETUP:

            # TODO: remove in favor of controller side argspec detecting valid arguments
            # network facts modules must support gather_subset
            name = self._connection.ansible_name.removeprefix('ansible.netcommon.')

            if name not in ('network_cli', 'httpapi', 'netconf'):
                subset = mod_args.pop('gather_subset', None)
                if subset not in ('all', ['all'], None):
                    self._display.warning('Not passing subset(%s) to %s' % (subset, fact_module))

            timeout = mod_args.pop('gather_timeout', None)
            if timeout is not None:
                self._display.warning('Not passing timeout(%s) to %s' % (timeout, fact_module))

            fact_filter = mod_args.pop('filter', None)
            if fact_filter is not None:
                self._display.warning('Not passing filter(%s) to %s' % (fact_filter, fact_module))

        # Strip out keys with ``None`` values, effectively mimicking ``omit`` behavior
        # This ensures we don't pass a ``None`` value as an argument expecting a specific type
        mod_args = dict((k, v) for k, v in mod_args.items() if v is not None)

        # handle module defaults
        resolved_fact_module = self._shared_loader_obj.module_loader.find_plugin_with_context(
            fact_module, collection_list=self._task.collections
        ).resolved_fqcn

        mod_args = _apply_action_arg_defaults(resolved_fact_module, self._task, mod_args, self._templar)

        return mod_args

    def _combine_task_result(self, result: dict[str, t.Any], task_result: dict[str, t.Any]) -> dict[str, t.Any]:
        """ builds the final result to return """
        filtered_res = {
            'ansible_facts': task_result.get('ansible_facts', {}),
            'warnings': task_result.get('warnings', []),
            'deprecations': task_result.get('deprecations', []),
        }

        # on conflict the last plugin processed wins, but try to do deep merge and append to lists.
        return merge_hash(result, filtered_res, list_merge='append_rp')

    def _handle_smart(self, modules: list, task_vars: dict[str, t.Any]):
        """ Updates the module list when 'smart' is used, lookup network os mappings or use setup, warn when things seem inconsistent """

        if 'smart' not in modules:
            return

        modules.pop(modules.index('smart'))  # remove as this will cause 'module not found' errors
        network_os = self._task.args.get('network_os', task_vars.get('ansible_network_os', task_vars.get('ansible_facts', {}).get('network_os')))

        if network_os:

            connection_map = C.config.get_config_value('CONNECTION_FACTS_MODULES', variables=task_vars)
            if network_os in connection_map:
                modules.append(connection_map[network_os])
            elif not modules:
                raise AnsibleActionFail(f"No fact modules available and we could not find a fact module for your network OS ({network_os}), "
                                        "try setting one via the `FACTS_MODULES` configuration.")

            if set(modules).intersection(set(C._ACTION_SETUP)):
                # most don't realize what setup works with networking connection plugins (forced_local)
                self._display.warning("Detected 'setup' module and a network OS is set, the output when running it will reflect 'localhost'"
                                      " and not the target when a networking connection plugin is used.")

        elif not set(modules).intersection(set(C._ACTION_SETUP)):
            # no network os and setup not in list, add setup by default since 'smart'
            modules.append('ansible.legacy.setup')

    # ------------------------------------------------------------------
    # per-subset fact cache orchestration
    # ------------------------------------------------------------------

    def _resolved_module_fqcn(self, fact_module: str) -> str:
        return self._shared_loader_obj.module_loader.find_plugin_with_context(
            fact_module, collection_list=self._task.collections
        ).resolved_fqcn

    def _excluded_subset_terms(self) -> set[str]:
        excluded: set[str] = set()
        subset = self._task.args.get('gather_subset')
        if isinstance(subset, str):
            subset = [subset]
        for term in subset or []:
            if isinstance(term, str) and term.startswith('!') and term != '!':
                excluded.add(term[1:])
        return excluded

    def _plan_subset_gather(self, modules: list[str], task_vars: dict[str, t.Any]):
        """Decide which fact modules execute and which subsets they must refresh.

        Returns ``(plan, ctx)``; ``ctx`` is None when the per-subset feature is inactive, in which
        case the caller behaves exactly as before (whole-record caching).
        """
        vm = self._variable_manager
        if vm is None or not vm.fact_subsets_active():
            # default whole-record behavior: every module executes, no subset metadata is produced
            return {fact_module: {
                'execute': True, 'kind': 'setup' if fact_module in C._ACTION_SETUP else 'module',
                'fqcn': fact_module, 'needed_terms': [], 'attrib_terms': [], 'subset_args': None,
            } for fact_module in modules}, None

        plan: dict[str, dict[str, t.Any]] = {}
        for fact_module in modules:
            plan[fact_module] = {
                'execute': True,
                'kind': 'setup' if fact_module in C._ACTION_SETUP else 'module',
                'fqcn': self._resolved_module_fqcn(fact_module),
                'needed_terms': [],   # declared subsets this run must (re)gather, driving freshness/failure
                'attrib_terms': [],   # terms used to attribute returned keys
                'subset_args': None,  # explicit gather_subset override for setup refresh
            }

        ttl_map, unavailable_policy = vm._resolve_fact_subset_policy()
        host = task_vars.get('inventory_hostname')
        record = vm.get_host_fact_record(host)
        meta = fact_subsets.get_meta(record)
        implicit = bool(getattr(self._task, '_implicit_gather', False))
        now = time.time()
        excluded = self._excluded_subset_terms()

        ctx = {
            'host': host,
            'record': record,
            'meta': meta,
            'ttl_map': ttl_map,
            'policy': unavailable_policy,
            'implicit': implicit,
            'now': now,
            'fresh_skipped': [],
        }

        if not implicit:
            # explicit gather tasks always run every module; provenance is updated for returned keys
            for fact_module, info in plan.items():
                if info['kind'] == 'setup':
                    info['attrib_terms'] = [term for term in ttl_map if '.' not in term]
                else:
                    info['attrib_terms'] = [info['fqcn']]
            return plan, ctx

        # implicit play gather. A missing/legacy record, or a record older than the whole-record
        # timeout, forces a full gather with the play's own gather_subset (the global backstop).
        whole_expired = vm.whole_record_expired(host)
        if whole_expired or not record.get('_ansible_facts_gathered', False) or meta is None:
            for fact_module, info in plan.items():
                if info['kind'] == 'setup':
                    info['needed_terms'] = [term for term in ttl_map if '.' not in term]
                    info['attrib_terms'] = list(info['needed_terms'])
                else:
                    declared = info['fqcn'] in ttl_map
                    info['needed_terms'] = [info['fqcn']] if (declared or whole_expired) else []
                    info['attrib_terms'] = [info['fqcn']]
            ctx['whole_record_expired'] = whole_expired
            return plan, ctx

        subsets_meta = meta['subsets']
        for fact_module, info in plan.items():
            if info['kind'] == 'setup':
                candidates = [term for term in ttl_map if '.' not in term and term not in excluded]
            elif info['fqcn'] in ttl_map and info['fqcn'] not in excluded:
                candidates = [info['fqcn']]
            else:
                # non-declared extra fact module: run as before whenever a gather happens
                info['execute'] = True
                info['attrib_terms'] = [info['fqcn']]
                continue

            stale = [term for term in candidates
                     if fact_subsets.is_stale(subsets_meta, term, ttl_map[term], now)]
            info['needed_terms'] = stale
            info['attrib_terms'] = candidates if info['kind'] == 'setup' else [info['fqcn']]
            info['execute'] = bool(stale)
            if info['kind'] == 'setup' and stale:
                info['subset_args'] = ['!all', '!min'] + stale
            if not info['execute']:
                ctx['fresh_skipped'].append(info['fqcn'])

        return plan, ctx

    def _run_fact_module(self, fact_module: str, mod_args: dict[str, t.Any], task_vars: dict[str, t.Any],
                         unavailable: dict[str, t.Any]) -> dict[str, t.Any] | None:
        """Execute one fact module, recording unreachable targets for subset policy instead of raising."""
        try:
            return self._execute_module(module_name=fact_module, module_args=mod_args, task_vars=task_vars, wrap_async=False)
        except AnsibleConnectionFailure as ex:
            unavailable[fact_module] = {'failed': True, 'msg': to_native(ex), 'unreachable': True}
            return None

    def _setup_filtered(self) -> bool:
        """True when this task filters setup facts (filtered output cannot prove facts vanished)."""
        return bool(self._task.args.get('filter'))

    def _finalize_subsets(self, plan: dict[str, dict[str, t.Any]], ctx: dict[str, t.Any] | None,
                          result: dict[str, t.Any], success: dict[str, dict[str, t.Any]],
                          failed: dict[str, t.Any], unavailable: dict[str, t.Any]) -> None:
        """Apply successful subsets with provenance and enforce the unavailable-gather policy."""
        if ctx is None:
            return

        vm = self._variable_manager
        host = ctx['host']
        ttl_map = ctx['ttl_map']
        policy = ctx['policy']
        entry = fact_subsets.ENTRY_IMPLICIT if ctx['implicit'] else fact_subsets.ENTRY_EXPLICIT
        now = time.time()
        batch_id = fact_subsets.make_batch_id()

        new_record = ctx['record']
        refreshed: list[str] = []

        for fact_module, info in plan.items():
            res = success.get(fact_module)
            if res is None:
                continue

            facts = dict(res.get('ansible_facts', {}) or {})
            collector_facts = facts.pop(fact_subsets.COLLECTOR_FACTS_KEY, None)

            if info['kind'] == 'setup' and collector_facts is not None:
                system = facts.get('ansible_system') or new_record.get('ansible_system')
                attribution, leftover = fact_subsets.attribute_collector_facts(
                    collector_facts, info['attrib_terms'], system)
                owned = {key for keys in attribution.values() for key in keys}
                extra = [key for key in facts
                         if key not in owned and not key.startswith('_ansible_') and key not in ('gather_subset', 'module_setup')]
                unmanaged = sorted(set(leftover) | set(extra))
                if unmanaged:
                    attribution[fact_subsets.UNMANAGED_SUBSET] = unmanaged
            elif info['kind'] == 'setup':
                # older targets do not report the collector channel: keep values, provenance unmanaged
                attribution = {fact_subsets.UNMANAGED_SUBSET: [key for key in facts if not key.startswith('_ansible_')]}
            else:
                attribution = {info['fqcn']: [key for key in facts if not key.startswith('_ansible_')]}

            attribution = {subset: keys for subset, keys in attribution.items() if keys}
            if attribution:
                prune = not (info['kind'] == 'setup' and self._setup_filtered())
                new_record = fact_subsets.apply_gather(
                    new_record, facts, attribution,
                    batch_id=batch_id, entry=entry, source=info['fqcn'], now=now, prune=prune)
                refreshed.append(info['fqcn'])

        # failures: decide per needed subset whether stale data can be served or the gather must fail
        stale_used: list[dict[str, t.Any]] = []
        original_meta = ctx['meta']
        original_subsets = original_meta.get('subsets', {}) if original_meta else {}

        for fact_module, info in plan.items():
            failure = failed.get(fact_module) or unavailable.get(fact_module)
            if failure is None:
                continue
            needed = info['needed_terms']
            if not needed:
                continue  # unrelated failure keeps the historical "module failed -> gather failed" behavior

            servable_terms = [term for term in needed if term in original_subsets]
            if policy == fact_subsets.POLICY_STALE and len(servable_terms) == len(needed):
                failed.pop(fact_module, None)
                unavailable.pop(fact_module, None)
                for term in needed:
                    subset_info = original_subsets[term]
                    gathered_at = subset_info.get('gathered_at')
                    ttl = ttl_map.get(term)
                    stale_info = {
                        'subset': term,
                        'source': info['fqcn'],
                        'batch_id': subset_info.get('batch_id'),
                        'gathered_at': subset_info.get('gathered_at'),
                        'age': round(now - gathered_at, 3) if isinstance(gathered_at, (int, float)) else None,
                        'expired_for': (round(now - gathered_at - ttl, 3)
                                        if isinstance(gathered_at, (int, float)) and ttl else None),
                    }
                    stale_used.append(stale_info)
                    self._display.warning(
                        "Fact subset %s from %s could not be re-gathered; using cached values that are "
                        "%s seconds past their TTL." % (term, info['fqcn'], stale_info['expired_for'])
                    )
            else:
                # fail policy (or no old value to serve): explicit failure, no data written for it
                if fact_module in unavailable:
                    failed[fact_module] = unavailable[fact_module]
                missing = [term for term in needed if term not in original_subsets]
                terms_text = ', '.join(sorted(set(needed)))
                if missing:
                    failed[fact_module]['missing_subsets'] = sorted(missing)
                # keep the module/connection error while stating which subsets were not refreshed
                original_msg = failed[fact_module].get('msg')
                suffix = (' and no previously cached values are available' if missing else '')
                failed[fact_module]['msg'] = (
                    "Failed to gather fact subset(s) %s via %s%s%s."
                    % (terms_text, info['fqcn'], suffix, (': %s' % original_msg) if original_msg else '')
                )

        # Unreachable modules without a stale-policy outcome (freshly skipped modules never reach here;
        # undeclared extra modules and failed-policy modules do) must not look like a successful gather.
        for fact_module in list(unavailable):
            if fact_module not in failed:
                failed[fact_module] = unavailable[fact_module]
                failed[fact_module].setdefault(
                    'msg', "Fact module %s was unreachable while gathering facts." % fact_module)

        if refreshed:
            new_record['_ansible_facts_gathered'] = True
            vm.save_host_fact_record(host, new_record)

        if stale_used:
            result[fact_subsets.RESULT_STALE_KEY] = stale_used

        result['fact_cache'] = {
            'batch_id': batch_id,
            'refreshed': sorted(set(refreshed)),
            'fresh_skipped': sorted(ctx['fresh_skipped']),
            'served_stale': [item['subset'] for item in stale_used],
        }

    def run(self, tmp: t.Optional[str] = None, task_vars: t.Optional[dict] = None) -> dict[str, t.Any]:

        result = super(ActionModule, self).run(tmp, task_vars)
        result['ansible_facts'] = {}

        # copy the value with list() so we don't mutate the config
        modules = list(C.config.get_config_value('FACTS_MODULES', variables=task_vars))
        self._handle_smart(modules, task_vars)

        parallel = task_vars.pop('ansible_facts_parallel', self._task.args.pop('parallel', None))

        # plan per-subset refresh; non-executable modules are served from the cache
        plan, ctx = self._plan_subset_gather(modules, task_vars)
        modules = [m for m in modules if plan[m]['execute']]

        success: dict[str, t.Any] = {}
        unavailable: dict[str, t.Any] = {}
        failed: dict[str, t.Any] = {}
        skipped: dict[str, t.Any] = {}

        if parallel is None:
            if len(modules) > 1:
                parallel = True
            else:
                parallel = False
        else:
            parallel = boolean(parallel)

        timeout = self._task.args.get('gather_timeout', None)
        async_val = self._task.async_val

        if modules and not parallel:
            # serially execute each module
            for fact_module in modules:
                # just one module, no need for fancy async
                mod_args = self._get_module_args(fact_module, task_vars)
                if plan[fact_module]['subset_args'] is not None:
                    mod_args['gather_subset'] = plan[fact_module]['subset_args']
                # TODO: use gather_timeout to cut module execution if module itself does not support gather_timeout
                if ctx is None:
                    # default whole-record behavior: connection failures propagate as unreachable
                    res = self._execute_module(module_name=fact_module, module_args=mod_args, task_vars=task_vars, wrap_async=False)
                else:
                    res = self._run_fact_module(fact_module, mod_args, task_vars, unavailable)
                    if res is None:
                        continue
                if res.get('failed', False):
                    failed[fact_module] = res
                elif res.get('skipped', False):
                    skipped[fact_module] = res
                else:
                    success[fact_module] = res
                    result = self._combine_task_result(result, res)

            self._remove_tmp_path(self._connection._shell.tmpdir)
        elif modules:
            # do it async, aka parallel
            jobs: dict[str, t.Any] = {}

            for fact_module in modules:
                mod_args = self._get_module_args(fact_module, task_vars)
                if plan[fact_module]['subset_args'] is not None:
                    mod_args['gather_subset'] = plan[fact_module]['subset_args']

                #  if module does not handle timeout, use timeout to handle module, hijack async_val as this is what async_wrapper uses
                # TODO: make this action complain about async/async settings, use parallel option instead .. or remove parallel in favor of async settings?
                if timeout and 'gather_timeout' not in mod_args:
                    self._task.async_val = int(timeout)
                elif async_val != 0:
                    self._task.async_val = async_val
                else:
                    self._task.async_val = 0

                self._display.vvvv("Running %s" % fact_module)
                try:
                    jobs[fact_module] = (self._execute_module(module_name=fact_module, module_args=mod_args, task_vars=task_vars, wrap_async=True))
                except AnsibleConnectionFailure as ex:
                    # default whole-record behavior propagates; subset policy records it as unavailable
                    if ctx is None:
                        raise
                    unavailable[fact_module] = {'failed': True, 'msg': to_native(ex), 'unreachable': True}

            while jobs:
                for module in jobs:
                    poll_args = {'jid': jobs[module]['ansible_job_id'], '_async_dir': os.path.dirname(jobs[module]['results_file'])}
                    try:
                        res = self._execute_module(module_name='ansible.legacy.async_status', module_args=poll_args, task_vars=task_vars, wrap_async=False)
                    except AnsibleConnectionFailure as ex:
                        if ctx is None:
                            raise
                        unavailable[module] = {'failed': True, 'msg': to_native(ex), 'unreachable': True}
                        del jobs[module]
                        break
                    if res.get('finished', False):
                        if res.get('failed', False):
                            failed[module] = res
                        elif res.get('skipped', False):
                            skipped[module] = res
                        else:
                            success[module] = res
                            result = self._combine_task_result(result, res)
                        del jobs[module]
                        break
                    else:
                        time.sleep(0.1)
                else:
                    time.sleep(0.5)

        # restore value for post processing
        if self._task.async_val != async_val:
            self._task.async_val = async_val

        if skipped:
            result['msg'] = f"The following modules were skipped: {', '.join(skipped.keys())}."
            result['skipped_modules'] = skipped
            if len(skipped) == len(modules):
                result['skipped'] = True  # deprecated: description='returning skipped from actions/modules' core_version='2.25'
                result['changed'] = False

        self._finalize_subsets(plan, ctx, result, success, failed, unavailable)

        if failed:
            result['failed_modules'] = failed

            errors = [r.get('exception') for r in failed.values() if isinstance(r, dict) and r.get('exception') is not None]
            if errors:
                result.update(_error_utils.result_dict_from_captured_errors(
                    msg=f"The following modules failed to execute: {', '.join(failed.keys())}.",
                    errors=errors,
                ))
            else:
                result['failed'] = True
                result['msg'] = f"The following modules failed to execute: {', '.join(failed.keys())}."

        # internal collector attribution channel never leaves the action
        result['ansible_facts'].pop(fact_subsets.COLLECTOR_FACTS_KEY, None)

        # tell executor facts were gathered
        result['ansible_facts']['_ansible_facts_gathered'] = True

        # hack to keep --verbose from showing all the setup module result
        result['_ansible_verbose_override'] = True

        return result
