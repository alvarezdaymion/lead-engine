"""Controlled diagnostics at the boundaries of local and external operations."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar


class SafeError(Exception):
    """A message constructed locally without untrusted exception text."""


class InputError(SafeError, ValueError):
    pass


class OperationError(SafeError, RuntimeError):
    pass


class MissingFile(SafeError, FileNotFoundError):
    pass


def diagnostic(error: Exception) -> str:
    if isinstance(error, SafeError):
        return str(error)
    try:
        status = getattr(error, "status_code", None)
    except Exception:
        status = None
    suffix = " (HTTP %d)" % status if type(status) is int and 100 <= status <= 599 else ""
    return "%s%s" % (type(error).__name__, suffix)


P = ParamSpec("P")
T = TypeVar("T")


def sanitized(function: Callable[P, T]) -> Callable[P, T]:
    @wraps(function)
    def call(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return function(*args, **kwargs)
        except SafeError:
            raise
        except Exception as error:
            kind = (MissingFile if isinstance(error, FileNotFoundError) else
                    InputError if isinstance(error, ValueError) else OperationError)
            raise kind(diagnostic(error)) from None
    return call
