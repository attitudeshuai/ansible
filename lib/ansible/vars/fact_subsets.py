# -*- coding: utf-8 -*-
# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import datetime as _datetime
import json
import time
import uuid

from collections.abc import Mapping

# A host fact record (the value stored under the host name in the fact cache) keeps its flat fact
# keys; gather-level provenance is stored alongside them under this internal key. Values stored under
# an ``_ansible_`` prefixed key are stripped from user visible results.
META_KEY = '_ansible_fact_cache_meta'

# Internal channel used by the setup module to report which fact keys each collector produced.
COLLECTOR_FACTS_KEY = '_ansible_collector_facts'

# Fact keys that cannot be attributed to a requested gather subset are recorded under this name.
UNMANAGED_SUBSET = 'unmanaged'

META_VERSION = 1

PROVENANCE_FULL = 'full'
PROVENANCE_LEGACY = 'legacy'
PROVENANCE_MISSING = 'missing'

STATE_FRESH = 'fresh'
STATE_STALE = 'stale'
STATE_MISSING = 'missing'
STATE_UNKNOWN = 'unknown'

ENTRY_IMPLICIT = 'implicit'
ENTRY_EXPLICIT = 'explicit'

POLICY_FAIL = 'fail'
POLICY_STALE = 'stale'

# Result field listing subsets served from expired values under the ``stale`` unavailable policy.
RESULT_STALE_KEY = 'stale_fact_subsets'


def make_batch_id() -> str:
    """Identifier of one gather run; shared by every subset/module gathered in that run."""
    return uuid.uuid4().hex


def empty_meta() -> dict:
    return {'version': META_VERSION, 'subsets': {}, 'fact_sources': {}}


def get_meta(record: object) -> dict | None:
    """Return validated gather metadata from a host record, or None for legacy/missing records."""
    if not isinstance(record, Mapping):
        return None

    meta = record.get(META_KEY)
    if not isinstance(meta, Mapping):
        return None

    if meta.get('version') != META_VERSION:
        return None

    subsets = meta.get('subsets')
    fact_sources = meta.get('fact_sources')
    if not isinstance(subsets, Mapping) or not isinstance(fact_sources, Mapping):
        return None

    return meta


def provenance(record: object) -> str:
    if record is None or not isinstance(record, Mapping):
        return PROVENANCE_MISSING
    if get_meta(record) is not None:
        return PROVENANCE_FULL
    return PROVENANCE_LEGACY


def strip_meta(record: object) -> dict:
    """Return a shallow copy of a host record with internal gather metadata removed (for injection)."""
    if not isinstance(record, Mapping):
        return {}
    return {k: v for k, v in record.items() if k != META_KEY}


def parse_subset_ttl(value: object) -> tuple[dict[str, int], list[str]]:
    """Parse the FACT_CACHE_SUBSET_TTL config value into a ``{subset: seconds}`` mapping.

    Accepts a mapping or a string holding JSON/YAML mapping or a comma separated ``name: seconds`` list.
    Returns the validated mapping and a list of human readable warnings for rejected entries.
    """
    warnings: list[str] = []

    if value is None:
        return {}, warnings

    if isinstance(value, Mapping):
        raw = dict(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return {}, warnings
        raw = None
        if text[0] in '{[':
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, Mapping):
                raw = dict(parsed)
        if raw is None:
            try:
                import yaml  # Ansible always vendors/requires PyYAML
                parsed = yaml.safe_load(text)
            except Exception:
                parsed = None
            if isinstance(parsed, Mapping):
                raw = dict(parsed)
        if raw is None and (':' in text or '=' in text):
            raw = {}
            for item in text.split(','):
                item = item.strip()
                if not item:
                    continue
                sep = ':' if ':' in item else '='
                name, sep, ttl_text = item.partition(sep)
                raw[name.strip()] = ttl_text.strip()
        if raw is None:
            return {}, [f"Ignoring invalid fact cache subset TTL value {text!r}: expected a mapping of subset names to seconds."]
    else:
        return {}, [f"Ignoring invalid fact cache subset TTL value of type {type(value).__name__!r}: expected a mapping."]

    ttl_map: dict[str, int] = {}
    for name, ttl in raw.items():
        if not isinstance(name, str) or not name:
            warnings.append(f"Ignoring invalid fact cache subset TTL entry {name!r}: subset name must be a non-empty string.")
            continue
        if name == 'all':
            warnings.append("Ignoring fact cache subset TTL entry 'all': use 'fact_caching_timeout' to expire the whole host record.")
            continue
        if name.startswith('!'):
            warnings.append(f"Ignoring fact cache subset TTL entry {name!r}: exclusion terms are not valid subset names.")
            continue
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float, str)):
            warnings.append(f"Ignoring fact cache subset TTL for {name!r}: value must be a number of seconds, got {ttl!r}.")
            continue
        try:
            ttl_seconds = int(str(ttl).strip())
        except (TypeError, ValueError):
            warnings.append(f"Ignoring fact cache subset TTL for {name!r}: value must be a number of seconds, got {ttl!r}.")
            continue
        if ttl_seconds < 0:
            warnings.append(f"Ignoring fact cache subset TTL for {name!r}: TTL must be 0 (never expire) or a positive number of seconds.")
            continue
        ttl_map[name] = ttl_seconds

    return ttl_map, warnings


def is_stale(subsets: Mapping, subset: str, ttl: int, now: float) -> bool:
    """A declared subset is stale when missing or older than its TTL; TTL of 0 never expires."""
    info = subsets.get(subset)
    if not isinstance(info, Mapping):
        return True
    if ttl == 0:
        return False
    gathered_at = info.get('gathered_at')
    if not isinstance(gathered_at, (int, float)) or isinstance(gathered_at, bool):
        return True
    return now - float(gathered_at) > ttl


def stale_subsets(meta: dict | None, ttl_map: Mapping[str, int], now: float | None = None) -> list[str]:
    """Return the sorted declared subset names that are missing or past their TTL."""
    if not ttl_map:
        return []
    now = time.time() if now is None else now
    subsets = (meta or {}).get('subsets', {}) if meta else {}
    return sorted(name for name, ttl in ttl_map.items() if is_stale(subsets, name, ttl, now))


def subset_state(meta: dict | None, subset: str, ttl: int | None, now: float | None = None) -> str:
    if meta is None:
        return STATE_UNKNOWN
    now = time.time() if now is None else now
    info = meta.get('subsets', {}).get(subset)
    if not isinstance(info, Mapping):
        return STATE_MISSING
    if ttl is not None and is_stale(meta.get('subsets', {}), subset, ttl, now):
        return STATE_STALE
    return STATE_FRESH


def apply_gather(
    record: object,
    facts: Mapping,
    attribution: Mapping[str, list[str]],
    *,
    batch_id: str,
    entry: str,
    source: str,
    now: float | None = None,
    prune: bool = True,
) -> dict:
    """Merge freshly gathered facts into a host record and update per-subset provenance.

    ``attribution`` maps subset names to the fact keys produced for them in this gather run. The
    mapping iteration order is honored on ownership conflicts (callers list explicitly requested
    subsets before dependency-only and unmanaged keys). Keys that belonged to a replaced subset
    but were not produced again are removed; keys owned by untouched subsets are preserved.

    ``prune=False`` keeps previously recorded subset keys that this run did not re-produce, used
    when the gather output was filtered (a missing key does not mean the fact vanished remotely).
    """
    now = time.time() if now is None else now
    new_record = strip_meta(record)
    new_record.update(facts)

    meta = get_meta(record)
    subsets: dict = dict(meta['subsets']) if meta else {}
    fact_sources: dict = dict(meta['fact_sources']) if meta else {}

    updated = set(attribution)
    produced = {key for keys in attribution.values() for key in keys}

    # Remove facts that vanished from the re-gathered subsets, keeping untouched subsets intact.
    # The unmanaged bucket is a union of incidental/legacy keys across gathers; never pruned here.
    # Filtered gathers cannot prove disappearance either, so pruning is skipped for those.
    if prune:
        for key in list(fact_sources):
            owner = fact_sources[key]
            if owner in updated and owner != UNMANAGED_SUBSET and key not in produced:
                fact_sources.pop(key, None)
                new_record.pop(key, None)

    # First claim in attribution order wins. Existing managed owners are preserved; the unmanaged
    # bucket yields when an explicit managed subset claims a key in a later gather.
    claimed: dict[str, str] = {}
    for subset_name, keys in attribution.items():
        for key in keys:
            if key in claimed:
                continue
            owner = fact_sources.get(key)
            if owner is not None and owner != UNMANAGED_SUBSET:
                continue
            claimed[key] = subset_name
    fact_sources.update(claimed)

    for subset_name, keys in attribution.items():
        owned = sorted({key for key, owner in fact_sources.items() if owner == subset_name})
        subsets[subset_name] = {
            'gathered_at': now,
            'batch_id': batch_id,
            'entry': entry,
            'source': source,
            'fact_keys': owned,
        }

    new_record[META_KEY] = {'version': META_VERSION, 'subsets': subsets, 'fact_sources': fact_sources}
    return new_record


def invalidate(record: object, subset: str | None = None) -> dict | None:
    """Invalidate one subset (drop its owned facts/provenance) or the whole record (subset=None)."""
    if subset is None:
        return None
    meta = get_meta(record)
    if meta is None:
        # Legacy records carry no per-key ownership; a subset invalidation cannot safely touch them.
        return dict(record) if isinstance(record, Mapping) else {}

    new_record = strip_meta(record)
    fact_sources = dict(meta['fact_sources'])
    subsets = dict(meta['subsets'])

    for key in list(fact_sources):
        if fact_sources[key] == subset:
            fact_sources.pop(key, None)
            new_record.pop(key, None)
    subsets.pop(subset, None)

    new_record[META_KEY] = {'version': META_VERSION, 'subsets': subsets, 'fact_sources': fact_sources}
    return new_record


def _iso_utc(epoch: float) -> str:
    return _datetime.datetime.fromtimestamp(epoch, tz=_datetime.timezone.utc).isoformat()


def _subset_entry(name: str, info: Mapping | None, ttl: int | None, now: float) -> dict:
    entry = {
        'name': name,
        'state': STATE_MISSING if info is None else STATE_FRESH,
        'ttl': ttl,
        'gathered_at': None,
        'gathered_at_epoch': None,
        'age': None,
        'expired_for': None,
        'batch_id': None,
        'entry': None,
        'source': None,
        'fact_keys': [],
    }
    if info is not None:
        gathered_at = info.get('gathered_at')
        entry.update({
            'gathered_at': _iso_utc(gathered_at) if isinstance(gathered_at, (int, float)) and not isinstance(gathered_at, bool) else None,
            'gathered_at_epoch': gathered_at,
            'batch_id': info.get('batch_id'),
            'entry': info.get('entry'),
            'source': info.get('source'),
            'fact_keys': list(info.get('fact_keys', [])),
        })
        if isinstance(gathered_at, (int, float)) and not isinstance(gathered_at, bool):
            age = now - float(gathered_at)
            entry['age'] = round(age, 3)
            if ttl is not None and ttl != 0 and age > ttl:
                entry['state'] = STATE_STALE
                entry['expired_for'] = round(age - ttl, 3)
    return entry


def known_gather_terms(all_collector_classes=None) -> frozenset[str]:
    """All gather_subset terms known across the builtin collectors (collector names and fact ids)."""
    if all_collector_classes is None:
        from ansible.module_utils.facts import default_collectors
        all_collector_classes = default_collectors.collectors

    terms = {'min', 'all'}
    for collector_class in all_collector_classes:
        if collector_class.name:
            terms.add(collector_class.name)
        terms.update(collector_class._fact_ids)
    return frozenset(terms)


def validate_subset_names(names, warnings: list[str], all_collector_classes=None) -> list[str]:
    """Reject TTL subset names that are neither known gather terms nor fact module FQCNs."""
    try:
        known = known_gather_terms(all_collector_classes)
    except ImportError:
        # Collector taxonomy is POSIX target data; if it cannot be imported on this controller,
        # leave the terms untouched and let a remote gather surface genuinely invalid ones.
        return list(names)

    valid = []
    for name in names:
        if name in known:
            valid.append(name)
        elif '.' in name:
            # namespaced fact module entries (e.g. ansible.builtin.package_facts) are valid subsets
            valid.append(name)
        else:
            warnings.append(f"Ignoring fact cache subset TTL for {name!r}: not a known gather subset or fact module FQCN.")
    return valid


def term_collector_names(term: str, system: str | None = None, all_collector_classes=None) -> frozenset[str]:
    """Resolve one gather subset term to the collector names it pulls in on the given platform."""
    if all_collector_classes is None:
        from ansible.module_utils.facts import default_collectors
        all_collector_classes = default_collectors.collectors
    from ansible.module_utils.facts import collector as fact_collector

    classes = fact_collector.collector_classes_from_gather_subset(
        all_collector_classes=all_collector_classes,
        gather_subset=['!all', '!min', term],
        platform_info={'system': system or 'Generic'},
    )
    return frozenset(cls.name for cls in classes if cls.name)


def _fact_key_matches_term(fact_key: str, term: str) -> bool:
    """Whether a produced fact key belongs to a fact_id term (same semantics as setup filters)."""
    deprefixed = fact_key[8:] if fact_key.startswith('ansible_') else fact_key
    return deprefixed == term or deprefixed.startswith(term)


def attribute_collector_facts(
    collector_facts: list[dict],
    terms: list[str],
    system: str | None = None,
    all_collector_classes=None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Attribute collector-produced fact keys to requested gather terms.

    A term may name a collector group (e.g. ``hardware``) or one of its fact ids (e.g. ``mounts``
    produced by the same hardware collector). Fact-id matches are specific and win over the group
    owning that collector; attribution is therefore independent of the configured term order.
    Keys not pulled in by any term are returned separately (the caller buckets them unmanaged).
    """
    attribution: dict[str, list[str]] = {term: [] for term in terms}
    leftover: list[str] = []

    try:
        term_map = {term: term_collector_names(term, system, all_collector_classes) for term in terms}
    except ImportError:
        # Collector taxonomy unavailable on this controller; gather values are kept as unmanaged.
        for entry in collector_facts or []:
            leftover.extend(entry.get('keys') or [])
        return attribution, leftover

    # terms equal to a collector name are groups; the rest address individual fact ids
    group_terms = [term for term in terms if any(term == name for name in term_map[term])]

    for entry in collector_facts or []:
        collector_name = entry.get('collector')
        for key in entry.get('keys') or []:
            specific = next((term for term in terms
                             if term not in group_terms
                             and collector_name in term_map[term]
                             and _fact_key_matches_term(key, term)), None)
            if specific is not None:
                attribution[specific].append(key)
            elif collector_name in group_terms:
                attribution[collector_name].append(key)
            else:
                leftover.append(key)

    # multiple collectors may report the same key; keep each attribution list unique and ordered
    attribution = {term: list(dict.fromkeys(keys)) for term, keys in attribution.items()}
    return attribution, list(dict.fromkeys(leftover))


def _fact_lookup_entry(name: str, state: str, subset: str | None) -> dict:
    return {
        'name': name, 'state': state, 'subset': subset,
        'gathered_at': None, 'gathered_at_epoch': None, 'age': None,
        'expired_for': None, 'batch_id': None, 'entry': None, 'source': None,
    }


def build_status(
    host: str,
    record: object,
    ttl_map: Mapping[str, int] | None = None,
    now: float | None = None,
    *,
    fact: str | None = None,
    subsets_filter: list[str] | None = None,
) -> dict:
    """Build the stable status structure shared by the task side and controller side entries."""
    now = time.time() if now is None else now
    ttl_map = dict(ttl_map or {})
    kind = provenance(record)

    status = {'host': host, 'provenance': kind, 'gathered': None, 'subsets': [], 'facts': {}}

    if kind == PROVENANCE_MISSING:
        status['gathered'] = False
        names = subsets_filter if subsets_filter is not None else list(ttl_map)
        status['subsets'] = [_subset_entry(name, None, ttl_map.get(name), now) for name in names]
        if fact is not None:
            status['facts'][fact] = _fact_lookup_entry(fact, STATE_MISSING, None)
        return status

    if isinstance(record, Mapping) and record.get('_ansible_facts_gathered'):
        status['gathered'] = True

    if kind == PROVENANCE_LEGACY:
        names = subsets_filter if subsets_filter is not None else list(ttl_map)
        status['subsets'] = [
            {**_subset_entry(name, None, ttl_map.get(name), now), 'state': STATE_UNKNOWN} for name in names
        ]
        if fact is not None:
            status['facts'][fact] = _fact_lookup_entry(fact, STATE_UNKNOWN, None)
        return status

    meta = get_meta(record)
    subsets_meta = meta['subsets']
    names = list(subsets_meta)
    for name in ttl_map:
        if name not in names and (subsets_filter is None or name in subsets_filter):
            names.append(name)
    if subsets_filter is not None:
        names = [name for name in names if name in subsets_filter]

    status['subsets'] = [
        _subset_entry(name, subsets_meta.get(name), ttl_map.get(name), now)
        for name in sorted(names)
    ]

    if fact is not None:
        owner = meta['fact_sources'].get(fact)
        info = subsets_meta.get(owner) if owner else None
        if info is None:
            fact_state = STATE_MISSING if fact not in record else STATE_UNKNOWN
            fact_entry = {'name': fact, 'state': fact_state, 'subset': owner,
                          'gathered_at': None, 'gathered_at_epoch': None, 'age': None,
                          'expired_for': None, 'batch_id': None, 'entry': None, 'source': None}
        else:
            fact_entry = {'name': fact, 'subset': owner}
            fact_entry.update({k: v for k, v in _subset_entry(owner, info, ttl_map.get(owner), now).items()
                               if k not in ('name', 'ttl', 'fact_keys')})
        status['facts'][fact] = fact_entry

    return status
