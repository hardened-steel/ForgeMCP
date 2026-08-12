"""Minimal application core for ForgeMCP."""

from __future__ import annotations

from typing import Any

from forgemcp.core.config import ForgeConfig
from forgemcp.core.services import ServiceRegistry

__all__ = ["ForgeApplication", "ForgeConfig", "LifecycleState", "ServiceRegistry"]


def __getattr__(name: str) -> Any:
    """Import the composition root lazily to keep feature error modules acyclic."""
    if name in {"ForgeApplication", "LifecycleState"}:
        from forgemcp.core.application import ForgeApplication, LifecycleState

        return {"ForgeApplication": ForgeApplication, "LifecycleState": LifecycleState}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
