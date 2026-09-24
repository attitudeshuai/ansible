# -*- coding: utf-8 -*-
# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from ansible.cli.facts import FactsCLI
from ansible.errors import AnsibleError, AnsibleOptionsError
from ansible.inventory.host import Host
from ansible.utils.context_objects import GlobalCLIArgs
from ansible.vars import fact_subsets as fs


def _cli(*args):
    # CLI args include the program name at index 0, like sys.argv.
    # Reset the args singleton because a real process only parses CLI args once.
    GlobalCLIArgs._Singleton__instance = None
    cli = FactsCLI(['ansible-facts', *args])
    cli.parse()
    return cli


def _status(host):
    return fs.build_status(host, {'_ansible_facts_gathered': True}, {}, now=1000.0)


def _prereqs(hosts):
    loader = MagicMock()
    inventory = MagicMock()
    inventory.get_hosts.return_value = [Host(name=h) for h in hosts]
    inventory.subset.return_value = None
    vm = MagicMock()
    vm.get_fact_cache_status.side_effect = lambda hostname, fact=None, subsets=None: _status(hostname)
    return loader, inventory, vm


def test_list_json_stable_structure(capsys):
    cli = _cli('list', 'h1', '--json')
    with patch.object(FactsCLI, '_play_prereqs', lambda self: _prereqs(['h1'])), \
            patch('ansible.cli.facts.sys.exit') as exit_mock:
        cli.run()

    payload = json.loads(capsys.readouterr().out)
    (status,) = payload['results']
    assert set(status) == {'host', 'provenance', 'gathered', 'subsets', 'facts'}
    assert status['host'] == 'h1'
    exit_mock.assert_called_once_with(0)


def test_list_human_table(capsys):
    cli = _cli('list', 'h1')
    with patch.object(FactsCLI, '_play_prereqs', lambda self: _prereqs(['h1'])), \
            patch('ansible.cli.facts.sys.exit'):
        cli.run()
    assert 'host h1 (provenance: legacy)' in capsys.readouterr().out


def test_invalidate_json(capsys):
    cli = _cli('invalidate', 'h1', '--subset', 'mounts', '--json')
    _, inventory, vm = _prereqs(['h1'])
    with patch.object(FactsCLI, '_play_prereqs', lambda self: (_, inventory, vm)), \
            patch('ansible.cli.facts.sys.exit'):
        cli.run()

    payload = json.loads(capsys.readouterr().out)
    assert payload['invalidated'] == {'h1': ['mounts']}
    vm.invalidate_facts.assert_called_once_with('h1', 'mounts')


def test_invalidate_whole_host(capsys):
    cli = _cli('invalidate', 'h1')
    _, inventory, vm = _prereqs(['h1'])
    with patch.object(FactsCLI, '_play_prereqs', lambda self: (_, inventory, vm)), \
            patch('ansible.cli.facts.sys.exit'):
        cli.run()
    vm.invalidate_facts.assert_called_once_with('h1', None)


def test_no_hosts_matched_errors():
    cli = _cli('list', 'nonexistent')
    _, inventory, vm = _prereqs([])
    with patch.object(FactsCLI, '_play_prereqs', lambda self: (_, inventory, vm)), \
            pytest.raises(AnsibleError, match='No hosts matched'):
        cli.run()


def test_fact_option_only_with_list():
    with pytest.raises(AnsibleOptionsError):
        _cli('invalidate', 'h1', '--fact', 'ansible_mounts')
