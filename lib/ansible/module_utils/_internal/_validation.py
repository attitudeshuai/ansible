from __future__ import annotations

import keyword

from collections.abc import Mapping
from typing import Any


# Parameter was supplied explicitly under its canonical option name.
ORIGIN_EXPLICIT = 'explicit'
# Parameter value was supplied through an alias; `ParameterOrigin.alias` records which alias was used.
ORIGIN_ALIAS = 'alias'
# Parameter value was filled from the argument spec declared default.
ORIGIN_DEFAULT = 'default'
# Parameter value was filled by a fallback; `ParameterOrigin.fallback` records the fallback strategy name.
ORIGIN_FALLBACK = 'fallback'
# Parameter value was filled by a default declared in a nested sub spec.
ORIGIN_SUB_SPEC_DEFAULT = 'sub_spec_default'
# Parameter value came from role defaults (task vars) rather than the role invocation; only used by the role entry point.
ORIGIN_ROLE_DEFAULT = 'role_default'


class ParameterOrigin:
    """Provenance of a single validated parameter value."""

    __slots__ = ('origin', 'alias', 'fallback')

    def __init__(self, origin: str, alias: str | None = None, fallback: str | None = None):
        self.origin = origin
        # Name of the alias through which the value was supplied, when origin is ORIGIN_ALIAS.
        self.alias = alias
        # Name of the fallback strategy that supplied the value, when origin is ORIGIN_FALLBACK.
        self.fallback = fallback

    def to_dict(self) -> dict[str, str | None]:
        """Return a JSON-native representation of the origin."""
        return {
            'origin': self.origin,
            'alias': self.alias,
            'fallback': self.fallback,
        }

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, ParameterOrigin):
            return NotImplemented
        return self.origin == other.origin and self.alias == other.alias and self.fallback == other.fallback

    def __repr__(self) -> str:
        details = self.origin
        if self.alias is not None:
            details = f'{details}:{self.alias}'
        elif self.fallback is not None:
            details = f'{details}:{self.fallback}'
        return f'ParameterOrigin({details!r})'


def record_parameter_origin(parameter_origins: dict[tuple, ParameterOrigin], option_path: tuple, origin: ParameterOrigin) -> None:
    """Record the origin of the option identified by the given path."""
    parameter_origins[tuple(option_path)] = origin


def get_recorded_origin(parameter_origins: dict[tuple, ParameterOrigin], option_path: tuple) -> ParameterOrigin | None:
    """Return the previously recorded origin at the path, or None when unrecorded."""
    return parameter_origins.get(tuple(option_path))


def get_seeded_origin(parameter_origins_seed: Any, option_path: tuple) -> ParameterOrigin | None:
    """Look up an externally provided origin for an input value at the path.

    The seed is a nested structure of mappings and lists whose leaves are ParameterOrigin objects.
    """
    current = parameter_origins_seed
    for component in option_path:
        if isinstance(current, Mapping):
            current = current.get(component)
        elif isinstance(current, list) and isinstance(component, int) and 0 <= component < len(current):
            current = current[component]
        else:
            return None
        if current is None:
            return None
    return current if isinstance(current, ParameterOrigin) else None


def ensure_input_parameter_origin(
    parameter_origins: dict[tuple, ParameterOrigin],
    option_path: tuple,
    parameter_origins_seed: Any = None,
) -> None:
    """Attribute an input value at the path once, using the external seed when present else explicit."""
    if tuple(option_path) in parameter_origins:
        return

    origin = get_seeded_origin(parameter_origins_seed, option_path) or ParameterOrigin(ORIGIN_EXPLICIT)
    record_parameter_origin(parameter_origins, option_path, origin)


def _materialize_origins(
    value: Any,
    option_path: tuple,
    parameter_origins: dict[tuple, ParameterOrigin],
) -> Any:
    path = tuple(option_path)
    has_descendants = any(len(recorded_path) > len(path) and tuple(recorded_path[:len(path)]) == path
                          for recorded_path in parameter_origins)

    if has_descendants:
        if isinstance(value, Mapping):
            return {
                key: _materialize_origins(sub_value, path + (key,), parameter_origins)
                for key, sub_value in value.items()
            }
        if isinstance(value, list):
            return [
                _materialize_origins(sub_value, path + (index,), parameter_origins)
                for index, sub_value in enumerate(value)
            ]

    return parameter_origins.get(path)


def materialize_parameter_origins(
    validated_parameters: Mapping,
    parameter_origins: dict[tuple, ParameterOrigin],
) -> dict[str, Any]:
    """Build a view of the origins with the same shape as the validated parameters.

    Dict options with sub specs nest into dicts and list-of-dict sub specs nest into per-element lists;
    leaves are ParameterOrigin objects.
    """
    return _materialize_origins(validated_parameters, (), parameter_origins)


def _origin_tree(value: Any, origin: str) -> Any:
    if isinstance(value, Mapping):
        return {key: _origin_tree(sub_value, origin) for key, sub_value in value.items()}
    if isinstance(value, list):
        return [_origin_tree(sub_value, origin) for sub_value in value]
    return ParameterOrigin(origin)


def _merge_origin_trees(defaults_value: Any, provided_value: Any) -> Any:
    if isinstance(defaults_value, Mapping) and isinstance(provided_value, Mapping):
        merged = _build_role_origin_seed(defaults_value, provided_value)
        return merged
    # Lists and scalars follow combine_vars semantics: provided values replace defaults wholesale.
    return _origin_tree(provided_value, ORIGIN_EXPLICIT)


def _build_role_origin_seed(defaults: Mapping, provided: Mapping) -> dict[str, Any]:
    seed = {}

    for key, defaults_value in defaults.items():
        if key in provided:
            seed[key] = _merge_origin_trees(defaults_value, provided[key])
        else:
            seed[key] = _origin_tree(defaults_value, ORIGIN_ROLE_DEFAULT)

    for key, provided_value in provided.items():
        if key not in defaults:
            seed[key] = _origin_tree(provided_value, ORIGIN_EXPLICIT)

    return seed


def build_role_parameter_origins(defaults: Mapping | None, provided: Mapping | None) -> dict[str, Any]:
    """Build a nested origin seed for the role validation entry point.

    Values present in `provided` (the role invocation) are attributed as explicit, while values only
    available from `defaults` (role defaults exposed as task vars) are attributed as role defaults.
    Nested mappings are merged; lists and scalars follow combine_vars precedence (provided replaces).
    """
    return _build_role_origin_seed(defaults or {}, provided or {})


def validate_collection_name(collection_name: object, name: str = 'collection_name') -> None:
    """Validate a collection name."""
    if not isinstance(collection_name, str):
        raise TypeError(f"{name} must be {str} instead of {type(collection_name)}")

    parts = collection_name.split('.')

    if len(parts) != 2 or not all(part.isidentifier() and not keyword.iskeyword(part) for part in parts):
        raise ValueError(f"{name} must consist of two non-keyword identifiers separated by '.'")
