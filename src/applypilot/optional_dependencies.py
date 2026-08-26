"""Small, explicit boundary for product capabilities backed by optional packages."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType


class OptionalDependencyError(RuntimeError):
    """Raised when a requested optional product capability is unavailable."""


def require_module(module_name: str, *, extra: str, purpose: str) -> ModuleType:
    """Import an optional module or raise one actionable product-level error."""
    try:
        return importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError) as exc:
        missing = getattr(exc, "name", None) or module_name
        raise OptionalDependencyError(
            f"{purpose} requires the optional '{extra}' dependencies "
            f"(missing or broken module: {missing}). Install with "
            f'`python -m pip install "applypilot-local[{extra}]"`.'
        ) from exc


def require_jobboards() -> ModuleType:
    """Return JobSpy on its supported interpreter range with actionable errors."""
    if sys.version_info >= (3, 13):
        raise OptionalDependencyError(
            "Broad third-party job-board discovery currently supports Python "
            "3.11-3.12 because python-jobspy pins NumPy 1.26. Use the core "
            "product on this interpreter, or run this capability in a Python "
            "3.11/3.12 environment."
        )
    return require_module(
        "jobspy",
        extra="jobboards",
        purpose="Broad third-party job-board discovery",
    )
