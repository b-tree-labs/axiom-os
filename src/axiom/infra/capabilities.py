# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Capability probing for the Axiom CLI.

A *capability* names an external dependency a command needs — the ``git``
binary, the ``gh`` CLI, a GitLab API token, and so on. Each capability
carries a *probe* that answers a single question: is this dependency
available in the current environment?

The availability-aware dispatcher (ADR-047) uses these probes to hide or
disable commands whose requirements are unmet, and to explain *why* with a
human ``reason`` and a ``remedy`` — rather than letting a command crash
mid-run on a missing dependency.

Probes are cheap but may be called repeatedly while argparse builds help,
so results are cached per process. Pass ``refresh=True`` to re-probe.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Availability:
    """Result of probing a capability."""

    available: bool
    reason: str = ""  # why it's unavailable (empty when available)
    remedy: str = ""  # how to make it available (install hint, env var, ...)


@dataclass(frozen=True)
class Capability:
    """A named external dependency with a probe for its availability."""

    name: str
    probe: Callable[[], Availability]
    description: str = ""


# ---------------------------------------------------------------------------
# Probe builders
# ---------------------------------------------------------------------------


def _binary_probe(binary: str, remedy: str) -> Callable[[], Availability]:
    """Build a probe that checks whether ``binary`` is on PATH."""

    def _probe() -> Availability:
        if shutil.which(binary):
            return Availability(True)
        return Availability(False, f"{binary!r} not found on PATH", remedy)

    return _probe


def _glab_config_token() -> str:
    """Read a GitLab token from the glab CLI config, if present."""
    cfg = Path.home() / ".config" / "glab-cli" / "config.yml"
    if not cfg.exists():
        return ""
    try:
        import yaml

        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    for host_data in (data.get("hosts") or {}).values():
        if isinstance(host_data, dict) and host_data.get("token"):
            return str(host_data["token"])
    return ""


def _gitlab_token_probe() -> Availability:
    if os.environ.get("GITLAB_TOKEN") or _glab_config_token():
        return Availability(True)
    return Availability(
        False,
        "no GitLab token found",
        "Set GITLAB_TOKEN or run `glab auth login`",
    )


# ---------------------------------------------------------------------------
# Built-in capabilities
# ---------------------------------------------------------------------------

GIT = Capability(
    "git",
    _binary_probe("git", "Install git: https://git-scm.com/downloads"),
    "git version-control binary",
)
GH_CLI = Capability(
    "gh",
    _binary_probe("gh", "Install the GitHub CLI: https://cli.github.com"),
    "GitHub CLI",
)
GLAB_CLI = Capability(
    "glab",
    _binary_probe("glab", "Install the GitLab CLI: https://gitlab.com/gitlab-org/cli"),
    "GitLab CLI",
)
GITLAB_TOKEN = Capability(
    "gitlab-token",
    _gitlab_token_probe,
    "GitLab API token",
)

_BUILTINS = (GIT, GH_CLI, GLAB_CLI, GITLAB_TOKEN)


# ---------------------------------------------------------------------------
# Registry + cached probing
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Capability] = {c.name: c for c in _BUILTINS}
_CACHE: dict[str, Availability] = {}


def register(cap: Capability) -> None:
    """Register a custom capability (e.g. contributed by an extension)."""
    _REGISTRY[cap.name] = cap


#: The entry-point group an installed extension contributes its capabilities through. Each entry
#: loads to a callable returning an iterable of :class:`Capability`; nothing is probed on load.
ENTRY_POINT_GROUP = "axiom.capabilities"

_DISCOVERED = False


def _entry_points() -> list:
    from importlib.metadata import entry_points

    return list(entry_points(group=ENTRY_POINT_GROUP))


def _discover() -> None:
    """Register the capabilities installed extensions contribute, once per process. Never raises:
    an extension whose contribution cannot load costs only its own capabilities."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    try:
        points = _entry_points()
    except Exception:  # noqa: BLE001
        return
    for point in points:
        try:
            for cap in point.load()() or ():
                if isinstance(cap, Capability) and cap.name not in _REGISTRY:
                    _REGISTRY[cap.name] = cap
        except Exception:  # noqa: BLE001
            continue


def get(name: str) -> Capability | None:
    """Return the registered capability named ``name``, or None."""
    if name not in _REGISTRY:
        _discover()
    return _REGISTRY.get(name)


def _resolve(cap: Capability | str) -> Capability:
    if isinstance(cap, Capability):
        return cap
    if cap not in _REGISTRY:
        _discover()
    resolved = _REGISTRY.get(cap)
    if resolved is None:
        raise KeyError(f"unknown capability: {cap!r}")
    return resolved


def check(cap: Capability | str, *, refresh: bool = False) -> Availability:
    """Probe ``cap`` (object or name), caching the result per process."""
    capability = _resolve(cap)
    if not refresh and capability.name in _CACHE:
        return _CACHE[capability.name]
    result = capability.probe()
    _CACHE[capability.name] = result
    return result


def is_available(cap: Capability | str, *, refresh: bool = False) -> bool:
    """True if ``cap`` is available."""
    return check(cap, refresh=refresh).available


def missing(
    requires: Iterable[Capability | str],
) -> list[tuple[Capability, Availability]]:
    """Return ``(capability, availability)`` for each unmet requirement."""
    unmet: list[tuple[Capability, Availability]] = []
    for req in requires:
        capability = _resolve(req)
        av = check(capability)
        if not av.available:
            unmet.append((capability, av))
    return unmet


def clear_cache() -> None:
    """Drop all cached probe results and re-read contributed capabilities (tests, long-lived
    processes)."""
    global _DISCOVERED
    _DISCOVERED = False
    _CACHE.clear()


__all__ = [
    "Availability",
    "Capability",
    "GIT",
    "GH_CLI",
    "GLAB_CLI",
    "GITLAB_TOKEN",
    "register",
    "get",
    "check",
    "is_available",
    "missing",
    "clear_cache",
]
