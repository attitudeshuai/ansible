# -*- coding: utf-8 -*-
# Copyright (c) 2026 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import pytest

from ansible.module_utils._internal._validation import (
    ORIGIN_ALIAS,
    ORIGIN_DEFAULT,
    ORIGIN_EXPLICIT,
    ORIGIN_FALLBACK,
    ORIGIN_ROLE_DEFAULT,
    ORIGIN_SUB_SPEC_DEFAULT,
    ParameterOrigin,
    build_role_parameter_origins,
    ensure_input_parameter_origin,
    get_recorded_origin,
    get_seeded_origin,
    materialize_parameter_origins,
    record_parameter_origin,
)


def test_parameter_origin_to_dict():
    assert ParameterOrigin(ORIGIN_EXPLICIT).to_dict() == {'origin': 'explicit', 'alias': None, 'fallback': None}
    assert ParameterOrigin(ORIGIN_ALIAS, alias='pkg').to_dict() == {'origin': 'alias', 'alias': 'pkg', 'fallback': None}
    assert ParameterOrigin(ORIGIN_FALLBACK, fallback='env_fallback').to_dict() == {
        'origin': 'fallback', 'alias': None, 'fallback': 'env_fallback',
    }


def test_parameter_origin_equality():
    assert ParameterOrigin(ORIGIN_DEFAULT) == ParameterOrigin(ORIGIN_DEFAULT)
    assert ParameterOrigin(ORIGIN_ALIAS, alias='a') != ParameterOrigin(ORIGIN_ALIAS, alias='b')
    assert ParameterOrigin(ORIGIN_SUB_SPEC_DEFAULT) != ParameterOrigin(ORIGIN_DEFAULT)


def test_record_and_get_origin():
    origins = {}
    record_parameter_origin(origins, ('name',), ParameterOrigin(ORIGIN_EXPLICIT))
    assert get_recorded_origin(origins, ('name',)) == ParameterOrigin(ORIGIN_EXPLICIT)
    assert get_recorded_origin(origins, ('missing',)) is None


def test_ensure_input_origin_without_seed():
    origins = {}
    ensure_input_parameter_origin(origins, ('name',))
    assert origins[('name',)] == ParameterOrigin(ORIGIN_EXPLICIT)

    # Explicitly recorded origins (alias/default/fallback) are not overwritten.
    record_parameter_origin(origins, ('other',), ParameterOrigin(ORIGIN_DEFAULT))
    ensure_input_parameter_origin(origins, ('other',))
    assert origins[('other',)] == ParameterOrigin(ORIGIN_DEFAULT)


def test_ensure_input_origin_with_seed():
    origins = {}
    seeds = {
        'a': ParameterOrigin(ORIGIN_ROLE_DEFAULT),
        'u': {'x': ParameterOrigin(ORIGIN_ROLE_DEFAULT)},
        'l': [{'x': ParameterOrigin(ORIGIN_ROLE_DEFAULT)}],
    }

    ensure_input_parameter_origin(origins, ('a',), seeds)
    ensure_input_parameter_origin(origins, ('u', 'x'), seeds)
    ensure_input_parameter_origin(origins, ('l', 0, 'x'), seeds)
    ensure_input_parameter_origin(origins, ('b',), seeds)

    assert origins[('a',)] == ParameterOrigin(ORIGIN_ROLE_DEFAULT)
    assert origins[('u', 'x')] == ParameterOrigin(ORIGIN_ROLE_DEFAULT)
    assert origins[('l', 0, 'x')] == ParameterOrigin(ORIGIN_ROLE_DEFAULT)
    assert origins[('b',)] == ParameterOrigin(ORIGIN_EXPLICIT)


def test_get_seeded_origin():
    seeds = {'u': [{'x': ParameterOrigin(ORIGIN_ROLE_DEFAULT)}]}
    assert get_seeded_origin(seeds, ('u', 0, 'x')) == ParameterOrigin(ORIGIN_ROLE_DEFAULT)
    assert get_seeded_origin(seeds, ('u', 5, 'x')) is None
    assert get_seeded_origin(seeds, ('u', 0, 'y')) is None


def test_materialize_scalar_and_nested():
    origins = {}
    validated = {
        'name': 'bo',
        'user': {'first': 'rey', 'age': 19},
        'users': [{'age': 1}, {'age': 2}],
        'tags': ['a', 'b'],
    }
    record_parameter_origin(origins, ('name',), ParameterOrigin(ORIGIN_EXPLICIT))
    record_parameter_origin(origins, ('user',), ParameterOrigin(ORIGIN_DEFAULT))
    record_parameter_origin(origins, ('user', 'first'), ParameterOrigin(ORIGIN_EXPLICIT))
    record_parameter_origin(origins, ('user', 'age'), ParameterOrigin(ORIGIN_SUB_SPEC_DEFAULT))
    record_parameter_origin(origins, ('users',), ParameterOrigin(ORIGIN_EXPLICIT))
    record_parameter_origin(origins, ('users', 0, 'age'), ParameterOrigin(ORIGIN_EXPLICIT))
    record_parameter_origin(origins, ('users', 1, 'age'), ParameterOrigin(ORIGIN_SUB_SPEC_DEFAULT))
    record_parameter_origin(origins, ('tags',), ParameterOrigin(ORIGIN_EXPLICIT))

    view = materialize_parameter_origins(validated, origins)

    assert view['name'] == ParameterOrigin(ORIGIN_EXPLICIT)
    assert view['user']['first'] == ParameterOrigin(ORIGIN_EXPLICIT)
    assert view['user']['age'] == ParameterOrigin(ORIGIN_SUB_SPEC_DEFAULT)
    assert view['users'][0]['age'] == ParameterOrigin(ORIGIN_EXPLICIT)
    assert view['users'][1]['age'] == ParameterOrigin(ORIGIN_SUB_SPEC_DEFAULT)
    # Plain list without sub spec options stays a leaf.
    assert view['tags'] == ParameterOrigin(ORIGIN_EXPLICIT)


@pytest.mark.parametrize(
    ('defaults', 'provided', 'key', 'expected'),
    (
        ({'a': 1}, {'b': 2}, 'a', ORIGIN_ROLE_DEFAULT),
        ({'a': 1}, {'b': 2}, 'b', ORIGIN_EXPLICIT),
        # Provided overrides defaults wholesale for scalars.
        ({'a': 1}, {'a': 2}, 'a', ORIGIN_EXPLICIT),
        # Nested mappings merge key by key.
        ({'u': {'x': 1, 'y': 2}}, {'u': {'y': 9}}, 'u', None),
        # Lists are replaced wholesale like combine_vars.
        ({'l': [1]}, {'l': [2, 3]}, 'l', None),
        # Empty inputs.
        (None, {'b': 2}, 'b', ORIGIN_EXPLICIT),
        ({'a': 1}, None, 'a', ORIGIN_ROLE_DEFAULT),
    ),
)
def test_build_role_parameter_origins(defaults, provided, key, expected):
    seeds = build_role_parameter_origins(defaults, provided)

    if key == 'u':
        assert seeds['u']['x'] == ParameterOrigin(ORIGIN_ROLE_DEFAULT)
        assert seeds['u']['y'] == ParameterOrigin(ORIGIN_EXPLICIT)
    elif key == 'l':
        assert [origin.origin for origin in seeds['l']] == [ORIGIN_EXPLICIT, ORIGIN_EXPLICIT]
    else:
        assert seeds[key] == ParameterOrigin(expected)
