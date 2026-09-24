# -*- coding: utf-8 -*-
# Copyright (c) 2026 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import pytest

from ansible.module_utils.errors import (
    AnsibleValidationError,
    AnsibleValidationErrorMultiple,
    ArgumentTypeError,
    RequiredError,
)


def test_validation_error_message_only():
    error = AnsibleValidationError('something went wrong')

    assert error.msg == 'something went wrong'
    assert error.error_message == 'something went wrong'
    assert error.option_path == ()
    assert error.rejected_value is None
    assert error.constraint is None

    assert error.details == {
        'option_path': [],
        'error_type': 'AnsibleValidationError',
        'message': 'something went wrong',
        'rejected_value': None,
        'constraint': None,
    }


def test_validation_error_details():
    error = ArgumentTypeError(
        'bad type',
        option_path=['users', 0, 'age'],
        rejected_value={'nope': True},
        constraint={'type': 'int'},
    )

    assert error.option_path == ('users', 0, 'age')

    assert error.details == {
        'option_path': ['users', 0, 'age'],
        'error_type': 'ArgumentTypeError',
        'message': 'bad type',
        'rejected_value': {'nope': True},
        'constraint': {'type': 'int'},
    }


def test_validation_error_subclass_type_name():
    error = RequiredError('missing', option_path=['name'])
    assert error.details['error_type'] == 'RequiredError'


def test_validation_multiple_details_order():
    first = ArgumentTypeError('first', option_path=['a'])
    second = RequiredError('second', option_path=['b'])
    errors = AnsibleValidationErrorMultiple([first, second])

    assert errors.msg == 'first'
    assert errors.messages == ['first', 'second']
    assert [d['message'] for d in errors.details] == ['first', 'second']
    assert [d['error_type'] for d in errors.details] == ['ArgumentTypeError', 'RequiredError']
    assert errors.details[0]['option_path'] == ['a']
    assert errors.details[1]['option_path'] == ['b']

    errors.append(RequiredError('third'))
    assert len(errors.details) == 3

    with pytest.raises(IndexError):
        AnsibleValidationErrorMultiple().msg
