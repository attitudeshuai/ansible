#!/usr/bin/env python
# Copyright: Contributors to the Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
# PYTHON_ARGCOMPLETE_OK

from __future__ import annotations

# ansible.cli needs to be imported first, to ensure the source bin/* scripts run that code first
from ansible.cli import CLI

import json
import sys

from ansible import context
from ansible.cli.arguments import option_helpers as opt_help
from ansible.errors import AnsibleError, AnsibleOptionsError
from ansible.module_utils.common.text.converters import to_text
from ansible.utils.display import Display

display = Display()


class FactsCLI(CLI):
    """List freshness of cached fact subsets and invalidate them, all on the controller."""

    name = 'ansible-facts'

    def init_parser(self):
        super().init_parser(desc='Inspect and invalidate per-subset fact cache records without connecting to targets.')

        opt_help.add_inventory_options(self.parser)
        opt_help.add_vault_options(self.parser)
        opt_help.add_basedir_options(self.parser)

        self.parser.add_argument('args', metavar='command [pattern]', nargs='+',
                                 help="The command (list or invalidate) and an optional host pattern (default: all).")
        self.parser.add_argument('--subset', action='append', default=None, dest='subsets', metavar='SUBSET',
                                 help="Gather subset to show or invalidate; repeat to name more than one.")
        self.parser.add_argument('--fact', default=None, dest='fact', metavar='FACT',
                                 help="Show provenance of this single fact (list command only).")
        self.parser.add_argument('--json', action='store_true', default=False, dest='json_output',
                                 help="Emit stable machine readable JSON instead of a table.")

    def post_process_args(self, options):
        options = super().post_process_args(options)
        display.verbosity = options.verbosity

        options.command = options.args[0]
        if options.command not in ('list', 'invalidate'):
            raise AnsibleOptionsError("Unknown command %r; valid commands are 'list' and 'invalidate'." % options.command)
        options.pattern = options.args[1] if len(options.args) > 1 else 'all'
        if len(options.args) > 2:
            raise AnsibleOptionsError("Unexpected extra arguments: %s" % ', '.join(options.args[2:]))

        if options.command == 'invalidate' and options.fact:
            raise AnsibleOptionsError("--fact can only be used with the 'list' command.")

        return options

    def run(self):
        super().run()

        loader, inventory, variable_manager = self._play_prereqs()

        if context.CLIARGS['subset']:
            inventory.subset(context.CLIARGS['subset'])

        hosts = inventory.get_hosts(context.CLIARGS['pattern'])
        if not hosts:
            raise AnsibleError("No hosts matched the pattern %r." % context.CLIARGS['pattern'])

        subsets = context.CLIARGS['subsets']
        fact = context.CLIARGS['fact']

        if context.CLIARGS['command'] == 'list':
            results = [
                variable_manager.get_fact_cache_status(host.get_name(), fact=fact, subsets=subsets)
                for host in hosts
            ]

            if context.CLIARGS['json_output']:
                display.display(json.dumps({'results': results}, indent=2, sort_keys=True, default=str))
            else:
                for status in results:
                    display.display(self._format_host_table(status))
        else:
            per_host = {}
            for host in hosts:
                hostname = host.get_name()
                if subsets:
                    for subset in subsets:
                        variable_manager.invalidate_facts(hostname, subset)
                    per_host[hostname] = list(subsets)
                else:
                    variable_manager.invalidate_facts(hostname, None)
                    per_host[hostname] = None

            if context.CLIARGS['json_output']:
                display.display(json.dumps({'invalidated': per_host}, indent=2, sort_keys=True))
            else:
                for hostname, invalidated in per_host.items():
                    if invalidated is None:
                        display.display("invalidated whole fact cache record of %s" % hostname)
                    else:
                        display.display("invalidated subset(s) %s of %s" % (', '.join(invalidated), hostname))

        sys.exit(0)

    @staticmethod
    def _format_host_table(status: dict) -> str:
        lines = ["host %s (provenance: %s)" % (status['host'], status['provenance'])]
        if not status['subsets']:
            lines.append("  (no subset information)")
        for entry in status['subsets']:
            gathered = entry['gathered_at'] or 'unknown'
            ttl = entry['ttl'] if entry['ttl'] is not None else '-'
            lines.append(
                "  %-24s state=%-7s ttl=%-8s gathered_at=%s batch=%s source=%s"
                % (entry['name'], entry['state'], ttl, gathered, entry['batch_id'] or '-', entry['source'] or '-')
            )
        for fact, info in (status.get('facts') or {}).items():
            lines.append(
                "  fact %-32s state=%-7s subset=%s gathered_at=%s batch=%s source=%s"
                % (to_text(fact), info['state'], info['subset'] or '-', info['gathered_at'] or 'unknown',
                   info['batch_id'] or '-', info['source'] or '-')
            )
        return "\n".join(lines)


def main(args=None):
    FactsCLI.cli_executor(args)


if __name__ == '__main__':
    main()
