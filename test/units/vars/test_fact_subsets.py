# -*- coding: utf-8 -*-
# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

from ansible.module_utils.facts.collector import BaseFactCollector
from ansible.vars import fact_subsets as fs


class _Collector(BaseFactCollector):
    def collect(self, module=None, collected_facts=None):
        return {}


class _Hardware(_Collector):
    name = 'hardware'
    _fact_ids = {'mounts', 'devices'}


class _Network(_Collector):
    name = 'network'
    _fact_ids = {'interfaces'}


class _Platform(_Collector):
    name = 'platform'


ALL_COLLECTORS = [_Hardware, _Network, _Platform]


def test_parse_subset_ttl_mapping():
    ttl, warnings = fs.parse_subset_ttl({'hardware': 100, 'network': 0})
    assert ttl == {'hardware': 100, 'network': 0}
    assert warnings == []


def test_parse_subset_ttl_string_forms():
    ttl, warnings = fs.parse_subset_ttl('hardware: 100, mounts: 10')
    assert ttl == {'hardware': 100, 'mounts': 10}
    ttl, warnings = fs.parse_subset_ttl('{"hardware": 100}')
    assert ttl == {'hardware': 100}


def test_parse_subset_ttl_rejects_invalid_entries():
    ttl, warnings = fs.parse_subset_ttl({'all': 5, 'bad': 'x', 'x': -3, '!network': 1, 'ok': 2})
    assert ttl == {'ok': 2}
    assert len(warnings) == 4


def test_stale_boundaries_and_zero_ttl():
    meta = fs.empty_meta()
    meta['subsets']['hardware'] = {'gathered_at': 100.0}
    assert fs.is_stale(meta['subsets'], 'hardware', 10, 109.0) is False
    assert fs.is_stale(meta['subsets'], 'hardware', 10, 110.0) is False   # exactly at the TTL is still fresh
    assert fs.is_stale(meta['subsets'], 'hardware', 10, 110.001) is True
    assert fs.is_stale(meta['subsets'], 'hardware', 0, 9_999_999.0) is False  # 0 never expires
    assert fs.is_stale(meta['subsets'], 'missing', 10, 101.0) is True
    assert fs.stale_subsets(meta, {'hardware': 10}, now=200.0) == ['hardware']


def test_apply_gather_replaces_and_preserves():
    first = fs.apply_gather(
        {}, {'ansible_mounts': 1, 'ansible_eth0': 2},
        {'hardware': ['ansible_mounts'], 'network': ['ansible_eth0']},
        batch_id='b1', entry=fs.ENTRY_IMPLICIT, source='ansible.legacy.setup', now=100.0)

    # a key that vanished from the replaced hardware subset disappears; network is untouched
    second = fs.apply_gather(
        first, {'ansible_mounts': 3}, {'hardware': ['ansible_mounts']},
        batch_id='b2', entry=fs.ENTRY_IMPLICIT, source='ansible.legacy.setup', now=200.0)

    assert second['ansible_mounts'] == 3
    assert 'ansible_eth0' in second
    meta = fs.get_meta(second)
    assert meta['subsets']['hardware']['batch_id'] == 'b2'
    assert meta['subsets']['hardware']['gathered_at'] == 200.0
    assert meta['subsets']['network']['batch_id'] == 'b1'
    assert meta['fact_sources'] == {'ansible_mounts': 'hardware', 'ansible_eth0': 'network'}


def test_apply_gather_buckets_unmanaged():
    record = fs.apply_gather(
        {}, {'ansible_a': 1, 'ansible_other': 2}, {'hardware': ['ansible_a'], fs.UNMANAGED_SUBSET: ['ansible_other']},
        batch_id='b', entry=fs.ENTRY_EXPLICIT, source='ansible.legacy.setup', now=1.0)
    assert fs.get_meta(record)['fact_sources']['ansible_other'] == fs.UNMANAGED_SUBSET


def test_strip_meta_is_non_mutating():
    record = fs.apply_gather({}, {'a': 1}, {'unmanaged': ['a']}, batch_id='b',
                             entry=fs.ENTRY_IMPLICIT, source='s', now=1.0)
    stripped = fs.strip_meta(record)
    assert fs.META_KEY not in stripped
    assert fs.META_KEY in record


def test_unmanaged_keys_are_union_and_yield_to_managed_subset():
    # first full gather: platform facts bucketed as unmanaged
    record = fs.apply_gather(
        {}, {'ansible_system': 'Linux', 'ansible_mounts': 1},
        {'mounts': ['ansible_mounts'], fs.UNMANAGED_SUBSET: ['ansible_system']},
        batch_id='b1', entry=fs.ENTRY_IMPLICIT, source='s', now=100.0)

    # refresh gather of mounts only: unmanaged ansible_system must not vanish
    record = fs.apply_gather(
        record, {'ansible_mounts': 2}, {'mounts': ['ansible_mounts']},
        batch_id='b2', entry=fs.ENTRY_IMPLICIT, source='s', now=200.0)
    assert record['ansible_system'] == 'Linux'
    assert fs.get_meta(record)['fact_sources']['ansible_system'] == fs.UNMANAGED_SUBSET

    # later the managed subset claims the key explicitly
    record = fs.apply_gather(
        record, {'ansible_system': 'Linux', 'ansible_mounts': 3},
        {'mounts': ['ansible_mounts', 'ansible_system']},
        batch_id='b3', entry=fs.ENTRY_IMPLICIT, source='s', now=300.0)
    assert fs.get_meta(record)['fact_sources']['ansible_system'] == 'mounts'


def test_apply_gather_prune_false_keeps_unproduced_keys():
    record = fs.apply_gather(
        {}, {'ansible_mounts': 1, 'ansible_devices': {}}, {'hardware': ['ansible_mounts', 'ansible_devices']},
        batch_id='b1', entry=fs.ENTRY_EXPLICIT, source='s', now=100.0)

    # filtered gather only re-produces one hardware fact: the other must not be pruned
    filtered = fs.apply_gather(
        record, {'ansible_mounts': 2}, {'hardware': ['ansible_mounts']},
        batch_id='b2', entry=fs.ENTRY_EXPLICIT, source='s', now=200.0, prune=False)
    assert filtered['ansible_devices'] == {}
    assert filtered['ansible_mounts'] == 2
    assert set(fs.get_meta(filtered)['subsets']['hardware']['fact_keys']) == {'ansible_mounts', 'ansible_devices'}


def test_invalidate_subset_and_host():
    record = fs.apply_gather(
        {}, {'ansible_mounts': 1, 'ansible_eth0': 2},
        {'hardware': ['ansible_mounts'], 'network': ['ansible_eth0']},
        batch_id='b', entry=fs.ENTRY_IMPLICIT, source='s', now=1.0)

    new_record = fs.invalidate(record, 'network')
    assert 'ansible_eth0' not in new_record
    assert 'ansible_mounts' in new_record
    assert 'network' not in fs.get_meta(new_record)['subsets']
    assert fs.get_meta(new_record)['subsets']['hardware']['gathered_at'] == 1.0

    assert fs.invalidate(record) is None  # host level invalidation
    assert fs.invalidate({'legacy': True}, 'network') == {'legacy': True}  # legacy record untouched


def test_status_full_legacy_missing():
    record = fs.apply_gather(
        {}, {'ansible_mounts': 1}, {'hardware': ['ansible_mounts']},
        batch_id='b1', entry=fs.ENTRY_IMPLICIT, source='ansible.legacy.setup', now=100.0)
    record['_ansible_facts_gathered'] = True

    full = fs.build_status('h', record, {'hardware': 10}, now=105.0, fact='ansible_mounts')
    assert full['provenance'] == fs.PROVENANCE_FULL
    assert full['subsets'][0]['state'] == fs.STATE_FRESH
    assert full['facts']['ansible_mounts']['subset'] == 'hardware'
    assert full['facts']['ansible_mounts']['batch_id'] == 'b1'

    stale = fs.build_status('h', record, {'hardware': 10}, now=200.0)
    assert stale['subsets'][0]['state'] == fs.STATE_STALE
    assert stale['subsets'][0]['expired_for'] == 90.0

    legacy = fs.build_status('h', {'ansible_x': 1, '_ansible_facts_gathered': True}, {'hardware': 10},
                             now=200.0, fact='ansible_x')
    assert legacy['provenance'] == fs.PROVENANCE_LEGACY
    assert legacy['facts']['ansible_x']['state'] == fs.STATE_UNKNOWN

    missing = fs.build_status('h', None, {'hardware': 10}, now=1.0, fact='ansible_mounts')
    assert missing['provenance'] == fs.PROVENANCE_MISSING
    assert missing['facts']['ansible_mounts']['state'] == fs.STATE_MISSING


def test_status_stable_keys():
    status = fs.build_status('h', None, {'hardware': 10}, now=1.0)
    assert set(status) == {'host', 'provenance', 'gathered', 'subsets', 'facts'}
    assert set(status['subsets'][0]) == {
        'name', 'state', 'ttl', 'gathered_at', 'gathered_at_epoch', 'age', 'expired_for',
        'batch_id', 'entry', 'source', 'fact_keys'}


def test_term_resolution_and_attribution_with_injected_collectors():
    assert 'hardware' in fs.term_collector_names('mounts', 'Linux', ALL_COLLECTORS)

    collector_facts = [
        {'collector': 'hardware', 'keys': ['ansible_mounts', 'ansible_devices']},
        {'collector': 'network', 'keys': ['ansible_eth0']},
        {'collector': 'platform', 'keys': ['ansible_system']},
    ]
    attribution, leftover = fs.attribute_collector_facts(collector_facts, ['mounts', 'network'], 'Linux', ALL_COLLECTORS)
    # fact-id terms only claim matching keys; other keys from the same collector are unmanaged here
    assert attribution == {'mounts': ['ansible_mounts'], 'network': ['ansible_eth0']}
    assert set(leftover) == {'ansible_system', 'ansible_devices'}


def test_group_and_its_fact_id_can_both_be_subsets_order_insensitive():
    """AC-2 headline scenario: hardware (group) and mounts (its fact id) coexist."""
    channel = [{'collector': 'hardware',
                'keys': ['ansible_mounts', 'ansible_devices', 'ansible_kernel']}]

    # the AC-2 configuration order: group TTL first, then the quickly changing fact id
    attr, leftover = fs.attribute_collector_facts(channel, ['hardware', 'mounts'], 'Linux', ALL_COLLECTORS)
    assert attr == {'hardware': ['ansible_devices', 'ansible_kernel'], 'mounts': ['ansible_mounts']}
    assert leftover == []

    # and the reverse order must attribute identically
    attr2, _ = fs.attribute_collector_facts(channel, ['mounts', 'hardware'], 'Linux', ALL_COLLECTORS)
    assert attr2 == attr


def test_validate_subset_names():
    warnings = []
    valid = fs.validate_subset_names(['hardware', 'bogus', 'ansible.builtin.package_facts'], warnings, ALL_COLLECTORS)
    assert valid == ['hardware', 'ansible.builtin.package_facts']
    assert len(warnings) == 1


def test_make_batch_id_unique():
    assert fs.make_batch_id() != fs.make_batch_id()
