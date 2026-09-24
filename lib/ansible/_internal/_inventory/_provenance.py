# Copyright (c) 2026 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""Provenance tracking for inventory source merging.

When a merge plan (or trace-only mode) is active, the :class:`MergeProvenance`
tracker records every inventory mutation performed through the standard
``InventoryData`` API while sources are parsed:

* host definitions,
* group definitions,
* host/group memberships,
* parent/child group relationships,
* host and group variables.

Every record is attributed to the current ``(source, plugin)`` parsing frame
managed by :class:`ansible.inventory.manager.InventoryManager`. When no tracker
is attached, ``InventoryData`` takes its historical zero-overhead code path.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import os
import typing as t

from ansible._internal._datatag._tags import Origin
from ansible.module_utils.common.text.converters import to_text
from ansible.utils.path import unfrackpath

from . import _mergeplan


class EntityType(enum.Enum):
    """Kind of inventory entity a variable or membership is attached to."""

    HOST = 'host'
    GROUP = 'group'


class ProposalKind(enum.Enum):
    """The kind of inventory mutation represented by a proposal."""

    HOST_DEFINED = 'host_defined'
    GROUP_DEFINED = 'group_defined'
    MEMBERSHIP = 'membership'
    CHILD_GROUP = 'child_group'
    VARIABLE = 'variable'


# adjudication outcomes
EFFECTIVE = 'effective'
OVERRIDDEN = 'overridden'
SHADOWED = 'shadowed_by_equal_value'


@dataclasses.dataclass(frozen=True, slots=True)
class Derivation:
    """Provenance metadata for results produced by a constructor-style plugin (e.g. ansible.builtin.constructed)."""

    step: str
    """Derivation step: ``compose``, ``groups`` or ``keyed_groups``."""

    expression: str
    """Expression key (compose var name, groups condition name) or keyed-group expression."""

    index: int | None
    """Entry index for list-based steps (keyed_groups), otherwise ``None``."""

    derived_from: tuple[str, ...]
    """Display names of the sources that had supplied the host when derivation occurred."""


@dataclasses.dataclass(slots=True)
class Proposal:
    """A single recorded inventory mutation."""

    seq: int
    order: tuple[int, ...]
    source: str | None
    source_name: str | None
    parent_source: str | None
    plugin: str | None
    kind: ProposalKind
    entity_type: EntityType | None
    entity: str | None
    name: str | None
    location: str
    internal: bool
    delegated_via: tuple[str, ...]
    derivation: Derivation | None
    strategy: str = 'last_wins'
    """Conflict strategy in effect at the source that produced this proposal."""

    value: t.Any = None
    """Raw variable value (variable proposals only); canonical comparison text is in ``canonical``."""

    canonical: str | None = None
    effective: bool = True
    reason: str | None = None

    @property
    def variable_key(self) -> tuple[EntityType, str, str]:
        return self.entity_type, self.entity, self.name


@dataclasses.dataclass(slots=True)
class SourceStatus:
    """Parse status of one top-level source or directory member."""

    source: str
    name: str
    parent_source: str | None
    kind: str
    order: tuple[int, ...]
    strategy: str
    declared: bool
    status: str = 'pending'
    plugin: str | None = None


@dataclasses.dataclass(slots=True)
class _Frame:
    source: str
    source_name: str
    parent_source: str | None
    kind: str
    order: tuple[int, ...]
    strategy: str
    declared: bool
    plugin: str | None = None
    internal: bool = False
    delegated_via: list[str] = dataclasses.field(default_factory=list)
    derivation: Derivation | None = None
    member_index: int = 0


def source_kind(source: str) -> str:
    """Classify a top-level source string for trace output."""
    if ',' in source:
        return 'host_list'
    b_source = os.fsencode(source)
    if os.path.isdir(b_source):
        return 'directory'
    if os.path.isfile(b_source):
        return 'file'
    return 'other'


def normalize_source(source: str) -> str:
    source = to_text(source)
    if ',' in source:
        return source
    return unfrackpath(source, follow=False)


def canonical_value(value: t.Any) -> str:
    """Return a deterministic, datatag-free canonical representation for value comparison."""
    # imported lazily so that merely importing InventoryData does not pull in the templating stack
    from ansible._internal._json import json_dumps_formatted

    try:
        return json_dumps_formatted(value)
    except Exception:
        # exotic values that the inventory JSON profile cannot serialize are compared by their native repr
        return repr(value)


def _origin_location(value: t.Any) -> str | None:
    try:
        origin = Origin.get_tag(value)
    except Exception:
        return None
    if origin is None:
        return None
    rendered = str(origin)
    return rendered or None


class MergeProvenance:
    """Holds parsing frames, the ordered proposal ledger and per-source status."""

    def __init__(self, ordered_sources: tuple[_mergeplan.OrderedSource, ...] = ()) -> None:
        self._frames: list[_Frame] = []
        self.ordered_sources: tuple[_mergeplan.OrderedSource, ...] = tuple(ordered_sources)
        self._ordered_lookup: dict[str, _mergeplan.OrderedSource] = {o.key: o for o in ordered_sources}
        self._ordered_names: dict[str, str] = {o.key: o.name for o in ordered_sources}
        self.proposals: list[Proposal] = []
        self.source_status: dict[str, SourceStatus] = {}
        self.adjudication: t.Any = None  # set by _adjudication.adjudicate()
        self._seq = 0
        self._dedup: set[tuple] = set()

    # ----- parsing frames -------------------------------------------------

    @contextlib.contextmanager
    def source_frame(self, source: str, *, parent_source: str | None = None, kind: str | None = None,
                     order: tuple[int, ...] | None = None, member: bool = False):
        source = to_text(source)
        # directory members are always their own (synthetic) source, even if the same path was also given as a top-level source
        declared = None if member else self._ordered_lookup.get(normalize_source(source))

        if declared is not None:
            frame = _Frame(
                source=source,
                source_name=declared.name,
                parent_source=parent_source,
                kind=kind or source_kind(source),
                order=(declared.order,) if order is None else order,
                strategy=declared.strategy.value,
                declared=True,
            )
        else:
            # directory member or other synthetic source; inherit the nearest declared ancestor's strategy
            ancestor_strategy = _mergeplan.MergeStrategy.LAST_WINS.value
            for ancestor in reversed(self._frames):
                if ancestor.declared:
                    ancestor_strategy = ancestor.strategy
                    break
            # directory members are always their own (synthetic) source, even if the same path was also given as a top-level source
            frame = _Frame(
                source=source,
                source_name=source,
                parent_source=parent_source,
                kind=kind or ('directory_member' if member else source_kind(source)),
                order=order if order is not None else (len(self.source_status),),
                strategy=ancestor_strategy,
                declared=False,
            )

        self._frames.append(frame)
        self.source_status[source] = SourceStatus(
            source=source,
            name=frame.source_name,
            parent_source=parent_source,
            kind=frame.kind,
            order=frame.order,
            strategy=frame.strategy,
            declared=frame.declared,
        )
        try:
            yield frame
        finally:
            self._frames.pop()

    @contextlib.contextmanager
    def plugin_context(self, plugin_name: str):
        frame = self._frames[-1] if self._frames else None
        if frame is None:
            yield
            return
        previous = frame.plugin
        frame.plugin = plugin_name
        try:
            yield
        finally:
            frame.plugin = previous

    @contextlib.contextmanager
    def internal_context(self):
        """Mark framework-generated writes (inventory_file/inventory_dir/ansible_port, reconcile edges) as internal."""
        frame = self._frames[-1] if self._frames else None
        if frame is None:
            yield
            return
        previous = frame.internal
        frame.internal = True
        try:
            yield
        finally:
            frame.internal = previous

    @contextlib.contextmanager
    def delegation_context(self, plugin_name: str, via: str = 'ansible.builtin.auto'):
        """Record that the active plugin delegates parsing to another plugin (the ``auto`` plugin)."""
        frame = self._frames[-1] if self._frames else None
        if frame is None:
            yield
            return
        prev_plugin = frame.plugin
        prev_delegated = list(frame.delegated_via)
        if via not in frame.delegated_via:
            frame.delegated_via.append(via)
        frame.plugin = plugin_name
        try:
            yield
        finally:
            frame.plugin = prev_plugin
            frame.delegated_via = prev_delegated

    @contextlib.contextmanager
    def derivation_context(self, *, step: str, expression: str, index: int | None = None,
                           derived_from: tuple[str, ...] = ()):
        frame = self._frames[-1] if self._frames else None
        if frame is None:
            yield
            return
        previous = frame.derivation
        frame.derivation = Derivation(step=step, expression=expression, index=index, derived_from=tuple(derived_from))
        try:
            yield
        finally:
            frame.derivation = previous

    def next_member_order(self) -> tuple[int, ...]:
        frame = self._frames[-1] if self._frames else None
        if frame is None:
            return (0,)
        index = frame.member_index
        frame.member_index += 1
        return frame.order + (index,)

    def mark_source(self, source: str, parsed: bool) -> None:
        status = self.source_status.get(to_text(source))
        if status is not None and status.status == 'pending':
            status.status = 'parsed' if parsed else 'unparsed'

    # ----- records --------------------------------------------------------

    def _record(self, *, kind: ProposalKind, entity_type: EntityType | None, entity: str | None, name: str | None,
                value: t.Any = None, dedupe_key: tuple | None = None, force_internal: bool = False,
                location_hint: t.Any = None) -> Proposal | None:
        if dedupe_key is not None and dedupe_key in self._dedup:
            return None
        if dedupe_key is not None:
            self._dedup.add(dedupe_key)

        frame = self._frames[-1] if self._frames else None
        internal = force_internal if force_internal else (frame.internal if frame else True)
        plugin = None if internal else (frame.plugin if frame else None)

        if internal:
            source = frame.source if frame else None
            source_name = frame.source_name if frame else None
            parent = frame.parent_source if frame else None
            order = frame.order if frame else (-1,)
            delegated = ()
            derivation = None
        else:
            source = frame.source
            source_name = frame.source_name
            parent = frame.parent_source
            order = frame.order
            delegated = tuple(frame.delegated_via)
            derivation = frame.derivation

        location = self._resolve_location(value, location_hint, frame, source, internal)

        self._seq += 1
        proposal = Proposal(
            seq=self._seq,
            order=order,
            source=source,
            source_name=source_name,
            parent_source=parent,
            plugin=plugin,
            kind=kind,
            entity_type=entity_type,
            entity=to_text(entity) if entity is not None else None,
            name=to_text(name) if name is not None else None,
            location=location,
            internal=internal,
            delegated_via=delegated,
            derivation=derivation,
            strategy=frame.strategy if frame else _mergeplan.MergeStrategy.LAST_WINS.value,
            value=value if kind is ProposalKind.VARIABLE else None,
            canonical=canonical_value(value) if kind is ProposalKind.VARIABLE else None,
        )
        self.proposals.append(proposal)
        return proposal

    @staticmethod
    def _resolve_location(value, hint, frame, source, internal) -> str:
        for candidate in (hint, value):
            location = _origin_location(candidate)
            if location:
                return location
        if frame is not None:
            if frame.kind == 'host_list':
                return f'<comma-separated host list from source {frame.source!r}>'
            if frame.source:
                return frame.source
        return '<internal>' if internal else '<unknown>'

    def record_host_defined(self, host: str) -> None:
        self._record(
            kind=ProposalKind.HOST_DEFINED, entity_type=EntityType.HOST, entity=host, name=None,
            dedupe_key=('host', self._frame_source(), host), location_hint=host,
        )

    def record_group_defined(self, group: str) -> None:
        self._record(
            kind=ProposalKind.GROUP_DEFINED, entity_type=EntityType.GROUP, entity=group, name=None,
            dedupe_key=('group', self._frame_source(), group), location_hint=group,
        )

    def record_membership(self, host: str, group: str) -> None:
        self._record(
            kind=ProposalKind.MEMBERSHIP, entity_type=EntityType.HOST, entity=host, name=group,
            dedupe_key=('membership', self._frame_source(), host, group),
        )

    def record_child_group(self, parent: str, child: str) -> None:
        self._record(
            kind=ProposalKind.CHILD_GROUP, entity_type=EntityType.GROUP, entity=parent, name=child,
            dedupe_key=('child_group', self._frame_source(), parent, child),
        )

    def record_variable(self, entity_type: EntityType, entity: str, varname: str, value: t.Any,
                        *, force_internal: bool = False) -> None:
        self._record(
            kind=ProposalKind.VARIABLE, entity_type=entity_type, entity=entity, name=varname,
            value=value, force_internal=force_internal, location_hint=varname,
        )

    def _frame_source(self) -> str | None:
        frame = self._frames[-1] if self._frames else None
        return frame.source if frame else None

    # ----- ledger queries used by adjudication and trace ------------------

    def proposals_for(self, kind: ProposalKind) -> list[Proposal]:
        return [p for p in self.proposals if p.kind is kind]

    def host_defining_sources(self, host: str) -> tuple[str, ...]:
        names: list[str] = []
        for proposal in sorted(
            (p for p in self.proposals if p.kind is ProposalKind.HOST_DEFINED and p.entity == host and not p.internal),
            key=lambda p: (p.order, p.seq),
        ):
            if proposal.source_name not in names:
                names.append(proposal.source_name)
        return tuple(names)

    def reset_adjudication(self) -> None:
        for proposal in self.proposals:
            proposal.effective = True
            proposal.reason = None
