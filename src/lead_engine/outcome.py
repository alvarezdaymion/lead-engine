"""What a pipeline step can return or raise."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import SafeError


@dataclass
class Advance:
    """The step succeeded: checkpoint `data` and move the lead to `stage`."""

    stage: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Drop:
    """The lead is proven unfit or unprovable. Terminal, with a reason."""

    reason: str
    detail: str = ""
    data: dict[str, Any] | None = None


class StepError(SafeError):
    """This lead's step failed in a way that may pass later (timeout, unreadable page, a draft that
    failed the voice rules). Counts toward the lead's repair budget."""


class SharedOutage(SafeError):
    """A dependency every lead needs is down (the model API is rate limited, the platform is failing).
    The lead is released without using its repair budget and the run stops early."""
