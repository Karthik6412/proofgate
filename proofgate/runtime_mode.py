"""Centralized runtime-mode resolution (Slice 20).

Resolves exactly one RuntimeMode per process from PROOFGATE_RUNTIME_MODE
(preferred) or, for backward compatibility only, the legacy per-integration
flags NEBIUS_LIVE_ENABLED / CRAFT_LIVE_ENABLED / DEMO_RELIABLE_MODE.

This module never changes agent.nebius_client.live_enabled() or
craft.config.live_enabled() -- both remain exactly as they were, reading
their own env vars independently, which is what all of their existing
tests already exercise directly. Instead, apply_runtime_mode_to_environment()
resolves the mode once and, only for FALLBACK/RELIABLE_DEMO, forces both
legacy flags to "false" so those existing, unchanged functions naturally
stay disabled. For LIVE, it deliberately does not touch either flag,
deferring entirely to each integration's own existing configuration check
and graceful degradation (agent/nebius_client.py and craft/evidence.py
already implement live-first-with-fallback correctly; this slice restores
that behavior by no longer hardcoding CRAFT off in app.py).

Reads the environment fresh on every call. Never makes a network call,
never reads or prints secret values, and has no import-time side effects
beyond configuring this module's own stderr logger.
"""

from __future__ import annotations

import logging
import os
import sys
from enum import Enum

logger = logging.getLogger("proofgate.runtime_mode")


def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_configure_logging()


class RuntimeMode(str, Enum):
    LIVE = "live"
    FALLBACK = "fallback"
    RELIABLE_DEMO = "reliable_demo"


class RuntimeModeError(ValueError):
    """Raised when PROOFGATE_RUNTIME_MODE or the legacy flags cannot be
    resolved to exactly one mode. Messages never include secret values --
    only the names and normalized values of configuration variables."""


_ALIASES: dict[str, RuntimeMode] = {
    "live": RuntimeMode.LIVE,
    "fallback": RuntimeMode.FALLBACK,
    "offline": RuntimeMode.FALLBACK,
    "reliable_demo": RuntimeMode.RELIABLE_DEMO,
    "reliable-demo": RuntimeMode.RELIABLE_DEMO,
    "demo": RuntimeMode.RELIABLE_DEMO,
}

_MODE_LABELS: dict[RuntimeMode, str] = {
    RuntimeMode.LIVE: "Live preferred",
    RuntimeMode.FALLBACK: "Fallback (deterministic)",
    RuntimeMode.RELIABLE_DEMO: "Reliable demo",
}


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes")


def _legacy_bool(name: str) -> bool | None:
    """True/False if name is explicitly present in the environment, else
    None (not explicitly configured -- distinct from "false")."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    return _truthy(raw)


def resolve_mode() -> RuntimeMode:
    """Resolve the single active RuntimeMode for this process.

    Precedence:
    1. PROOFGATE_RUNTIME_MODE, if explicitly set. Invalid values raise
       RuntimeModeError rather than silently defaulting.
    2. Legacy flags, mapped deterministically, only for backward
       compatibility:
       - DEMO_RELIABLE_MODE truthy -> RELIABLE_DEMO.
       - NEBIUS_LIVE_ENABLED / CRAFT_LIVE_ENABLED: if either is explicitly
         set, both must agree (both true-like, or both false-like) --
         disagreement is rejected rather than guessed at, since a single
         resolved mode cannot represent "one integration on, the other
         off". A lone explicit flag (the other left at its own default)
         maps conservatively: false -> FALLBACK, true -> LIVE.
    3. Default: LIVE.

    A deprecation warning is logged to stderr whenever a legacy flag was
    the deciding factor (never when PROOFGATE_RUNTIME_MODE was used).
    """
    explicit = os.environ.get("PROOFGATE_RUNTIME_MODE")
    if explicit:
        key = explicit.strip().lower()
        if key not in _ALIASES:
            raise RuntimeModeError(
                f"Invalid PROOFGATE_RUNTIME_MODE={explicit!r}. Must be one "
                "of: live, fallback, reliable_demo."
            )
        return _ALIASES[key]

    if _legacy_bool("DEMO_RELIABLE_MODE"):
        logger.warning(
            "DEMO_RELIABLE_MODE is a deprecated legacy flag; set "
            "PROOFGATE_RUNTIME_MODE=reliable_demo instead."
        )
        return RuntimeMode.RELIABLE_DEMO

    nebius_flag = _legacy_bool("NEBIUS_LIVE_ENABLED")
    craft_flag = _legacy_bool("CRAFT_LIVE_ENABLED")
    explicit_flags = [flag for flag in (nebius_flag, craft_flag) if flag is not None]

    if explicit_flags:
        if len(set(explicit_flags)) > 1:
            raise RuntimeModeError(
                "Contradictory legacy configuration: NEBIUS_LIVE_ENABLED and "
                "CRAFT_LIVE_ENABLED disagree. Set PROOFGATE_RUNTIME_MODE "
                "explicitly (live, fallback, or reliable_demo) instead."
            )
        logger.warning(
            "NEBIUS_LIVE_ENABLED/CRAFT_LIVE_ENABLED are deprecated legacy "
            "flags; set PROOFGATE_RUNTIME_MODE instead."
        )
        return RuntimeMode.LIVE if explicit_flags[0] else RuntimeMode.FALLBACK

    return RuntimeMode.LIVE


def apply_runtime_mode_to_environment() -> RuntimeMode:
    """Resolve the active mode and, for FALLBACK/RELIABLE_DEMO, force the
    existing legacy per-integration flags to "false" so
    agent.nebius_client.live_enabled() and craft.config.live_enabled()
    (both unchanged by this slice) naturally stay disabled. For LIVE,
    deliberately leaves both flags untouched, deferring to each
    integration's own existing configuration and graceful degradation.

    Safe to call at process startup or module import time: reads/writes
    only os.environ, makes no network call, and never logs a secret
    value.
    """
    mode = resolve_mode()
    if mode in (RuntimeMode.FALLBACK, RuntimeMode.RELIABLE_DEMO):
        os.environ["NEBIUS_LIVE_ENABLED"] = "false"
        os.environ["CRAFT_LIVE_ENABLED"] = "false"
    return mode


def mode_label(mode: RuntimeMode) -> str:
    """Judge-readable label for the resolved mode."""
    return _MODE_LABELS[mode]
