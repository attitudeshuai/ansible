# Copyright (c) 2026 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""Cross-source variable adjudication for declared inventory merge plans.

After every source has been parsed (so constructor plugins such as
``ansible.builtin.constructed`` could see their predecessors), the recorded
variable proposals are replayed per source:

* ``last_wins``  - the historical behavior; the latest differing source wins
                   (dict values still merge per ``combine_vars``),
* ``first_wins`` - the earliest source keeps the variable wholesale,
* ``error``      - all value conflicts are collected and a single aggregated
                   error is raised before reconcile/vars-plugin post-processing.

Proposals with equal canonical values never conflict; the later sources are
recorded as ``shadowed_by_equal_value``. Non-effective differing proposals are
recorded as ``overridden``.
"""

from __future__ import annotations

import dataclasses
import typing as t

from collections.abc import Mapping, MutableMapping

from ansible.errors import AnsibleError
from ansible.inventory.group import InventoryObjectType
from ansible.utils.vars import combine_vars

from . import _mergeplan, _provenance as _p


@dataclasses.dataclass(slots=True)
class VarOverride:
    """A variable supplied by multiple sources where differing values were resolved by a strategy."""

    entity_type: _p.EntityType
    entity: str
    variable: str
    strategy: str
    winner: _p.Proposal
    losers: list[_p.Proposal]


@dataclasses.dataclass(slots=True)
class VarConflict:
    """A variable whose differing cross-source values could not be merged under the ``error`` strategy."""

    entity_type: _p.EntityType
    entity: str
    variable: str
    entries: list[_p.Proposal]
    """All per-source proposals involved, in effective source order."""


class InventoryMergeConflictError(AnsibleError):
    """Aggregated error raised after all sources parsed when the ``error`` strategy found conflicts."""

    def __init__(self, result: AdjudicationResult) -> None:
        self.result = result
        super().__init__(result.format_conflicts())


@dataclasses.dataclass(slots=True)
class AdjudicationResult:
    overrides: list[VarOverride]
    conflicts: list[VarConflict]

    def format_conflicts(self) -> str:
        lines = ['Inventory merge conflict(s) detected for variable(s) supplied by multiple inventory sources:']
        for conflict in self.conflicts:
            kind = 'host' if conflict.entity_type is _p.EntityType.HOST else 'group'
            lines.append(f"- {kind} {conflict.entity!r} variable {conflict.variable!r}:")
            for proposal in conflict.entries:
                lines.append(
                    f"    source {proposal.source_name!r} ({proposal.source!r}, plugin "
                    f"{proposal.plugin or 'internal'}, at {proposal.location}): {proposal.canonical}"
                )
        lines.append("Resolve the conflicting values, or change the conflict strategy in the inventory merge plan.")
        return '\n'.join(lines)


def _assign(obj: t.Any, varname: str, value: t.Any) -> None:
    """Replay one effective variable write using the same merge/replace rules as Host/Group.set_variable."""
    existing = obj.vars.get(varname)
    if isinstance(existing, MutableMapping) and isinstance(value, Mapping):
        obj.vars = combine_vars(obj.vars, {varname: value})
    else:
        obj.vars[varname] = value
    # mirror Group.set_variable's special handling
    if obj.base_type is InventoryObjectType.GROUP and varname == 'ansible_group_priority':
        obj.set_priority(value)


def adjudicate(tracker: _p.MergeProvenance, inventory_data: t.Any) -> AdjudicationResult:
    """Resolve every cross-source variable conflict, annotate proposals and rebuild entity variables."""

    tracker.reset_adjudication()

    variable_proposals = [p for p in tracker.proposals if p.kind is _p.ProposalKind.VARIABLE]
    variable_proposals.sort(key=lambda p: (p.order, p.seq))

    # collapse to one proposal per (variable, source): the last in-source write (preserves in-source semantics)
    grouped: dict[tuple, list[_p.Proposal]] = {}
    for proposal in variable_proposals:
        grouped.setdefault(proposal.variable_key, []).append(proposal)

    per_source: dict[tuple, list[_p.Proposal]] = {}
    for key, proposals in grouped.items():
        last_by_source: dict[str | None, _p.Proposal] = {}
        for proposal in proposals:
            last_by_source[proposal.source] = proposal
        per_source[key] = list(last_by_source.values())  # dict preserves first-insertion (= effective source) order

    overrides: list[VarOverride] = []
    conflicts: list[VarConflict] = []
    winners: dict[tuple, _p.Proposal] = {}
    # replay behavior per variable:
    # 'last'  - replay all sources in order (dict hashes merge across sources, scalars replace): the historical rule
    # 'first' - replay only the winning (first) source wholesale
    replay_mode: dict[tuple, str] = {}

    for key, proposals in per_source.items():
        if len(proposals) == 1:
            winners[key] = proposals[0]
            replay_mode[key] = 'last'
            continue

        winner = proposals[0]
        conflict_entries: list[_p.Proposal] = [winner]
        divergence_strategy: str | None = None
        mode = 'last'

        for challenger in proposals[1:]:
            if challenger.canonical == winner.canonical:
                # equal value: never a conflict, the earlier definition stays effective
                challenger.effective = False
                challenger.reason = _p.SHADOWED
                continue

            strategy = challenger.strategy
            if divergence_strategy is None:
                divergence_strategy = strategy

            if strategy == _mergeplan.MergeStrategy.ERROR.value:
                conflict_entries.append(challenger)
                # advance the cursor so further value transitions are also surfaced
                winner = challenger
            elif strategy == _mergeplan.MergeStrategy.FIRST_WINS.value:
                challenger.effective = False
                challenger.reason = _p.OVERRIDDEN
                # the earliest source stays effective; if no later source wins, only it is replayed
                if mode != 'last':
                    mode = 'first'
            else:  # last_wins
                challenger.effective = True
                winner = challenger
                mode = 'last'

        if conflict_entries and len({p.canonical for p in conflict_entries}) > 1:
            conflicts.append(VarConflict(
                entity_type=key[0], entity=key[1], variable=key[2], entries=list(conflict_entries),
            ))
            winners[key] = conflict_entries[-1]
            continue

        # final classification against the surviving winner value
        losers: list[_p.Proposal] = []
        for proposal in proposals:
            if proposal is winner:
                proposal.effective = True
                proposal.reason = None
            else:
                proposal.effective = False
                if proposal.reason is None:
                    proposal.reason = _p.OVERRIDDEN if proposal.canonical != winner.canonical else _p.SHADOWED
                if proposal.reason == _p.OVERRIDDEN:
                    losers.append(proposal)

        winners[key] = winner
        replay_mode[key] = mode
        if losers:
            overrides.append(VarOverride(
                entity_type=key[0], entity=key[1], variable=key[2],
                strategy=divergence_strategy or winner.strategy, winner=winner, losers=losers,
            ))

    result = AdjudicationResult(overrides=overrides, conflicts=conflicts)
    tracker.adjudication = result

    if conflicts:
        # do not rebuild partial inventory; the caller raises before reconcile/vars-plugin post-processing
        return result

    # rebuild host/group variables by replaying recorded writes:
    # * 'last' variables replay every source in order, exactly reproducing the historical merge/replace rules
    #   (only equal-value shadow sources are skipped as their content is identical);
    # * 'first' variables replay only the winning source wholesale.
    shadow_sources: dict[tuple, set[str | None]] = {}
    for key, proposals in per_source.items():
        shadow_sources[key] = {p.source for p in proposals if p.reason == _p.SHADOWED}

    replayed_entities: set[tuple] = set()
    for proposal in variable_proposals:
        key = proposal.variable_key
        winner = winners[key]
        if replay_mode[key] == 'first':
            if proposal.source != winner.source:
                continue
        elif proposal.source in shadow_sources[key]:
            continue
        if proposal.entity_type is _p.EntityType.HOST:
            obj = inventory_data.hosts.get(proposal.entity)
        else:
            obj = inventory_data.groups.get(proposal.entity)
        if obj is None:
            continue
        identity = (proposal.entity_type, proposal.entity)
        if identity not in replayed_entities:
            # framework-internal variables (inventory_file/inventory_dir/ansible_port, ...) are
            # proposals as well, so clearing and replaying preserves them
            obj.vars = {}
            replayed_entities.add(identity)
        _assign(obj, proposal.name, proposal.value)

    return result
