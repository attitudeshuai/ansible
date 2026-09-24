# -*- coding: utf-8 -*-
# Copyright (c) 2021 Ansible Project
# Simplified BSD License (see licenses/simplified_bsd.txt or https://opensource.org/licenses/BSD-2-Clause)

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class AnsibleFallbackNotFound(Exception):
    """Fallback validator was not found"""


class AnsibleValidationError(Exception):
    """Single argument spec validation error"""

    def __init__(
        self,
        message: str,
        option_path: Sequence[str | int] | None = None,
        rejected_value: Any = None,
        constraint: dict[str, Any] | None = None,
    ):
        super(AnsibleValidationError, self).__init__(message)
        self.error_message = message
        """The error message passed in when the exception was raised."""

        # Path of option names and list indices identifying where the error occurred; empty tuple is the spec top level.
        self.option_path = tuple(option_path or ())
        # The rejected value; None when the error has no single rejected value (missing param, cross-param constraints).
        self.rejected_value = rejected_value
        # The violated declaration constraint projected to JSON-native data, or None for non-declaration errors.
        self.constraint = constraint

    @property
    def msg(self):
        """The error message passed in when the exception was raised."""
        return self.args[0]

    @property
    def details(self) -> dict[str, Any]:
        """Structured, JSON-native record describing the validation failure.

        Contains the option path, error type (exception class name), unchanged message text, rejected value and constraint.
        """
        return {
            'option_path': list(self.option_path),
            'error_type': type(self).__name__,
            'message': self.msg,
            'rejected_value': self.rejected_value,
            'constraint': self.constraint,
        }


class AnsibleValidationErrorMultiple(AnsibleValidationError):
    """Multiple argument spec validation errors"""

    def __init__(self, errors: list[AnsibleValidationError] | None = None):
        self.errors = errors[:] if errors else []
        """:class:`list` of :class:`AnsibleValidationError` objects"""

    def __getitem__(self, key):
        return self.errors[key]

    def __setitem__(self, key, value):
        self.errors[key] = value

    def __delitem__(self, key):
        del self.errors[key]

    @property
    def msg(self):
        """The first message from the first error in ``errors``."""
        return self.errors[0].args[0]

    @property
    def messages(self) -> list[str]:
        """:class:`list` of each error message in ``errors``."""
        return [err.msg for err in self.errors]

    @property
    def details(self) -> list[dict[str, Any]]:
        """:class:`list` of structured records from each error in ``errors``, in append order."""
        return [err.details for err in self.errors]

    def append(self, error):
        """Append a new error to ``self.errors``.

        Only :class:`AnsibleValidationError` should be added.
        """

        self.errors.append(error)

    def extend(self, errors):
        """Append each item in ``errors`` to ``self.errors``. Only :class:`AnsibleValidationError` should be added."""
        self.errors.extend(errors)


class AliasError(AnsibleValidationError):
    """Error handling aliases"""


class ArgumentTypeError(AnsibleValidationError):
    """Error with parameter type"""


class ArgumentValueError(AnsibleValidationError):
    """Error with parameter value"""


class DeprecationError(AnsibleValidationError):
    """Error processing parameter deprecations"""


class ElementError(AnsibleValidationError):
    """Error when validating elements"""


class MutuallyExclusiveError(AnsibleValidationError):
    """Mutually exclusive parameters were supplied"""


class NoLogError(AnsibleValidationError):
    """Error converting no_log values"""


class RequiredByError(AnsibleValidationError):
    """Error with parameters that are required by other parameters"""


class RequiredDefaultError(AnsibleValidationError):
    """A required parameter was assigned a default value"""


class RequiredError(AnsibleValidationError):
    """Missing a required parameter"""


class RequiredIfError(AnsibleValidationError):
    """Error with conditionally required parameters"""


class RequiredOneOfError(AnsibleValidationError):
    """Error with parameters where at least one is required"""


class RequiredTogetherError(AnsibleValidationError):
    """Error with parameters that are required together"""


class SubParameterTypeError(AnsibleValidationError):
    """Incorrect type for subparameter"""


class UnsupportedError(AnsibleValidationError):
    """Unsupported parameters were supplied"""
