# -*- coding: utf-8 -*-

# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations


DOCUMENTATION = r"""
---
module: fact_cache
short_description: Inspect and invalidate per-subset fact cache records on the controller
description:
  - This controller-side action queries the gather provenance of cached host facts (subset, last
    successful gather time, gather batch and source fact module) and invalidates subsets or whole
    host records.
  - It never connects to the target host and never gathers facts.
  - Per-subset freshness is only available when FACT_CACHE_SUBSET_TTL is configured and the fact
    cache plugin supports subset records (the builtin V(jsonfile) and V(memory) plugins do).
  - Records written by older Ansible versions without gather metadata are reported as V(unknown);
    they are neither treated as empty data nor cause an error.
version_added: "2.22"
options:
  state:
    description:
      - V(status) returns the freshness structure of the host, subsets or a single fact.
      - V(invalidate) removes the given subsets (or the whole host record when O(subset) is omitted)
        from the fact cache.
    type: str
    choices: [status, invalidate]
    default: status
  host:
    description:
      - Host whose cache record is inspected or invalidated. Defaults to the current host.
    type: str
  subset:
    description:
      - Gather subset name(s) to restrict the status output, or to invalidate.
      - With V(state=status) this only filters the output; with V(state=invalidate) the named subsets
        are removed while all other subsets of the host stay intact.
    type: list
    elements: str
  fact:
    description:
      - Name of a fact (including its C(ansible_) prefix when applicable) to return provenance for.
    type: str
attributes:
  action:
    support: full
  become:
    support: none
  connection:
    support: none
  check_mode:
    support: full
  diff_mode:
    support: none
  platform:
    platforms: all
extends_documentation_fragment:
  - action_common_attributes
  - action_common_attributes.flow
author:
  - Ansible Core Team
"""

EXAMPLES = r"""
- name: Show freshness of every cached subset of the current host
  ansible.builtin.fact_cache:
    state: status

- name: Show provenance of one fact
  ansible.builtin.fact_cache:
    state: status
    fact: ansible_mounts

- name: Invalidate only the mounts subset so it is re-gathered next run
  ansible.builtin.fact_cache:
    state: invalidate
    subset:
      - mounts

- name: Inspect another host record
  ansible.builtin.fact_cache:
    state: status
    host: db1.example.com
"""

RETURN = r"""
status:
  description: Stable freshness structure (returned with state=status).
  returned: when state=status
  type: dict
  contains:
    host:
      description: Host the record belongs to.
      type: str
    provenance:
      description: full (gather metadata present), legacy (older record) or missing (no record).
      type: str
    subsets:
      description: Per-subset freshness entries.
      type: list
      elements: dict
    facts:
      description: Per-fact provenance, keyed by the requested fact name.
      type: dict
invalidated:
  description: Mapping of host to the invalidated subsets, or null for a whole host invalidation.
  returned: when state=invalidate
  type: dict
"""
