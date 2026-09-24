# Copyright (c) 2026 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""Inventory merge-plan model, loading and pre-parse validation.

A merge plan declares the order in which inventory sources are parsed and the
per-source conflict strategy used when the same variable is supplied by more
than one source. This module is internal; the only public entry point is the
``merge_plan`` keyword argument accepted by
:class:`ansible.inventory.manager.InventoryManager`.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import typing as t

from collections.abc import Mapping

from ansible.errors import AnsibleError, AnsibleOptionsError
from ansible.module_utils.common.text.converters import to_native, to_text
from ansible.utils.path import unfrackpath

if t.TYPE_CHECKING:
    from ansible.parsing.dataloader import DataLoader


class MergeStrategy(enum.Enum):
    """Conflict strategy applied when a later source supplies a variable already defined by an earlier source."""

    FIRST_WINS = 'first_wins'
    """The value supplied by the earliest ordered source is kept; later sources are recorded as overridden."""

    LAST_WINS = 'last_wins'
    """The value supplied by the latest ordered source wins (the historical behavior)."""

    ERROR = 'error'
    """Parsing completes for every source, then a single aggregated error is raised for all value conflicts."""


_DEFAULT_STRATEGY = MergeStrategy.LAST_WINS
_SUPPORTED_VERSIONS = frozenset({1})
_STRATEGY_VALUES = frozenset(strategy.value for strategy in MergeStrategy)
_TOP_LEVEL_KEYS = frozenset({'version', 'default_strategy', 'sources'})
_SOURCE_KEYS = frozenset({'path', 'name', 'strategy'})


@dataclasses.dataclass(frozen=True, slots=True)
class SourcePlan:
    """One ordered source declaration from a merge plan."""

    path: str
    """Source path or string as written in the plan; matched against the sources supplied via ``-i``/API."""

    name: str
    """Human-friendly label used in trace output and error messages; defaults to :attr:`path`."""

    strategy: MergeStrategy | None
    """Per-source strategy override; ``None`` means the plan default strategy applies."""


@dataclasses.dataclass(frozen=True, slots=True)
class MergePlan:
    """Validated merge plan."""

    version: int
    default_strategy: MergeStrategy
    sources: tuple[SourcePlan, ...]
    """Sources in declared order."""


@dataclasses.dataclass(frozen=True, slots=True)
class OrderedSource:
    """One source in the effective parse order, with its resolved strategy."""

    source: str
    """Original source string, as supplied via ``-i``/API; used to drive ``parse_source``."""

    key: str
    """Normalized matching key (``unfrackpath`` for path sources, raw string for comma host lists)."""

    name: str
    """Display name (from the plan when declared, otherwise the source string)."""

    strategy: MergeStrategy
    """Strategy in effect when this source conflicts with an earlier one."""

    declared: bool
    """True when the source was listed in the merge plan; False for an undeclared source appended at the end."""

    order: int
    """Zero-based position in the effective parse order."""


@dataclasses.dataclass(frozen=True, slots=True)
class ResolvedPlan:
    """Merge plan resolved against the set of sources actually supplied for this run."""

    plan: MergePlan
    ordered_sources: tuple[OrderedSource, ...]
    """All supplied sources in effective parse order (declared order first, undeclared sources after, CLI order preserved)."""


def source_key(source: str) -> str:
    """Return the normalized key used to match plan entries against sources supplied via ``-i``/API."""
    source = to_text(source)
    if ',' in source:
        # comma-separated host lists are parsed inline and never touch the filesystem; match the raw string
        return source
    return unfrackpath(source, follow=False)


def _coerce_strategy(value: t.Any, *, where: str) -> MergeStrategy:
    if not isinstance(value, str) or value not in _STRATEGY_VALUES:
        valid = ', '.join(sorted(_STRATEGY_VALUES))
        raise AnsibleOptionsError(
            f"Invalid inventory merge plan: {where} has invalid conflict strategy {value!r}; valid values are: {valid}."
        )
    return MergeStrategy(value)


def _parse_plan(data: t.Any) -> MergePlan:
    if not isinstance(data, Mapping):
        raise AnsibleOptionsError(
            "Invalid inventory merge plan: expected a YAML mapping with 'version', 'default_strategy' and 'sources' keys, "
            f"got {type(data).__name__} instead."
        )

    unknown_keys = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown_keys:
        raise AnsibleOptionsError(
            f"Invalid inventory merge plan: unknown top-level key(s) {', '.join(unknown_keys)!r}; "
            f"allowed keys are: {', '.join(sorted(_TOP_LEVEL_KEYS))}."
        )

    version = data.get('version', 1)
    if isinstance(version, bool) or not isinstance(version, int) or version not in _SUPPORTED_VERSIONS:
        valid = ', '.join(str(v) for v in sorted(_SUPPORTED_VERSIONS))
        raise AnsibleOptionsError(
            f"Invalid inventory merge plan: unsupported 'version' {version!r}; supported versions are: {valid}."
        )

    default_strategy = _DEFAULT_STRATEGY
    if 'default_strategy' in data:
        default_strategy = _coerce_strategy(data['default_strategy'], where="'default_strategy'")

    raw_sources = data.get('sources', [])
    if not isinstance(raw_sources, list):
        raise AnsibleOptionsError(
            f"Invalid inventory merge plan: 'sources' must be a list, got {type(raw_sources).__name__} instead."
        )

    entries: list[SourcePlan] = []
    for index, raw_entry in enumerate(raw_sources):
        where = f"sources[{index}]"
        if not isinstance(raw_entry, Mapping):
            raise AnsibleOptionsError(
                f"Invalid inventory merge plan: {where} must be a mapping, got {type(raw_entry).__name__} instead."
            )

        unknown_entry_keys = sorted(set(raw_entry) - _SOURCE_KEYS)
        if unknown_entry_keys:
            raise AnsibleOptionsError(
                f"Invalid inventory merge plan: {where} has unknown key(s) {', '.join(unknown_entry_keys)!r}; "
                f"allowed keys are: {', '.join(sorted(_SOURCE_KEYS))}."
            )

        path = raw_entry.get('path')
        if not isinstance(path, str) or not path:
            raise AnsibleOptionsError(
                f"Invalid inventory merge plan: {where} requires a non-empty string 'path'."
            )

        name = raw_entry.get('name', path)
        if not isinstance(name, str) or not name:
            raise AnsibleOptionsError(
                f"Invalid inventory merge plan: {where} has an invalid 'name' {name!r}; expected a non-empty string."
            )

        strategy = None
        if 'strategy' in raw_entry:
            strategy = _coerce_strategy(raw_entry['strategy'], where=f"{where}.strategy")

        entries.append(SourcePlan(path=path, name=name, strategy=strategy))

    return MergePlan(version=version, default_strategy=default_strategy, sources=tuple(entries))


def resolve_order(plan: MergePlan, sources: t.Iterable[str]) -> ResolvedPlan:
    """Validate plan references against the sources actually supplied and compute the effective parse order."""

    cli_sources = [to_text(s) for s in sources if s]
    available: dict[str, str] = {}
    for source in cli_sources:
        available.setdefault(source_key(source), source)

    ordered: list[OrderedSource] = []
    seen: set[str] = set()

    for index, entry in enumerate(plan.sources):
        key = source_key(entry.path)

        if key not in available:
            available_list = '\n  - '.join(cli_sources) if cli_sources else '(none)'
            raise AnsibleOptionsError(
                f"Invalid inventory merge plan: sources[{index}] ({entry.name!r}, path {entry.path!r}) does not match any "
                f"inventory source supplied for this run. Available sources:\n  - {available_list}"
            )

        if key in seen:
            raise AnsibleOptionsError(
                f"Invalid inventory merge plan: source {entry.path!r} is declared more than once; "
                "each inventory source may appear in 'sources' at most once."
            )

        ordered.append(OrderedSource(
            source=available[key],
            key=key,
            name=entry.name,
            strategy=entry.strategy or plan.default_strategy,
            declared=True,
            order=index,
        ))
        seen.add(key)

    # undeclared sources keep their CLI-relative order and parse after the declared ones with legacy last_wins semantics
    for source in cli_sources:
        key = source_key(source)
        if key not in seen:
            ordered.append(OrderedSource(
                source=source,
                key=key,
                name=source,
                strategy=MergeStrategy.LAST_WINS,
                declared=False,
                order=len(ordered),
            ))
            seen.add(key)

    return ResolvedPlan(plan=plan, ordered_sources=tuple(ordered))


def default_order(sources: t.Iterable[str]) -> ResolvedPlan:
    """Build the effective order for trace-only mode: CLI order with legacy last_wins semantics."""
    plan = MergePlan(version=1, default_strategy=_DEFAULT_STRATEGY, sources=())
    return resolve_order(plan, sources)


def load_merge_plan(
    plan: str | os.PathLike[str] | Mapping[str, t.Any] | MergePlan,
    *,
    sources: t.Iterable[str],
    loader: DataLoader | None = None,
) -> ResolvedPlan:
    """Load, validate and resolve a merge plan against the supplied inventory sources.

    ``plan`` may be a path to a YAML file, an already parsed mapping, or a :class:`MergePlan`.
    All validation errors are raised before this function returns, i.e. before any inventory
    plugin is invoked.
    """

    if isinstance(plan, MergePlan):
        parsed = plan
    elif isinstance(plan, (str, os.PathLike)):
        plan_path = to_text(os.fspath(plan))

        if loader is None:
            from ansible.parsing.dataloader import DataLoader

            loader = DataLoader()

        try:
            data = loader.load_from_file(plan_path, cache='none')
        except AnsibleError:
            raise
        except Exception as ex:
            raise AnsibleOptionsError(
                f"Unable to read inventory merge plan {plan_path!r}: {to_native(ex)}"
            ) from ex

        if data is None:
            raise AnsibleOptionsError(f"Invalid inventory merge plan: {plan_path!r} is empty.")

        try:
            parsed = _parse_plan(data)
        except AnsibleOptionsError as ex:
            raise AnsibleOptionsError(f"{ex._message} (from merge plan file {plan_path!r})") from ex
    elif isinstance(plan, Mapping):
        parsed = _parse_plan(plan)
    else:
        raise AnsibleOptionsError(
            "Invalid inventory merge plan: expected a path, a parsed mapping or a MergePlan instance, "
            f"got {type(plan).__name__} instead."
        )

    return resolve_order(parsed, sources)
