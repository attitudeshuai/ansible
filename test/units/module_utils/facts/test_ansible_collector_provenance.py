# -*- coding: utf-8 -*-
# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

from ansible.module_utils.facts.ansible_collector import AnsibleFactCollector, COLLECTOR_FACTS_KEY
from ansible.module_utils.facts.collector import BaseFactCollector


class _GoodCollector(BaseFactCollector):
    name = 'good'
    _fact_ids = set()

    def collect(self, module=None, collected_facts=None):
        return {'ansible_good': 1, 'plain_good': 2}


class _FailingCollector(BaseFactCollector):
    name = 'failing'
    _fact_ids = set()

    def collect(self, module=None, collected_facts=None):
        raise RuntimeError('kaboom')


def test_collect_reports_keys_per_collector():
    collector = AnsibleFactCollector(collectors=[_GoodCollector()])
    facts = collector.collect()

    assert facts['ansible_good'] == 1
    assert facts[COLLECTOR_FACTS_KEY] == [{'collector': 'good', 'keys': ['ansible_good', 'plain_good']}]


def test_collect_attributes_survive_collector_failures():
    collector = AnsibleFactCollector(collectors=[_FailingCollector(), _GoodCollector()])
    facts = collector.collect()

    assert facts['ansible_good'] == 1
    report = {entry['collector']: entry['keys'] for entry in facts[COLLECTOR_FACTS_KEY]}
    assert 'failing' in report and report['failing'] == []
    assert report['good'] == ['ansible_good', 'plain_good']
