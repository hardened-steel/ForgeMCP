"""Minimal application core for ForgeMCP."""

from forgemcp.core.application import ForgeApplication, LifecycleState
from forgemcp.core.config import ForgeConfig
from forgemcp.core.services import ServiceRegistry

__all__ = ["ForgeApplication", "ForgeConfig", "LifecycleState", "ServiceRegistry"]
