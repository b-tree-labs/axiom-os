# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""HookRegistry — manifest + filesystem discovery for platform hooks.

Walks every installed extension via `axiom.extensions.discovery.discover_extensions()`
and finds `[[extension.provides]] kind = "hook"` blocks. Routes each event to
either `HookBus.register` (interceptor) or `EventBus.subscribe` (observer)
based on the closed taxonomy in `event_schemas.PLATFORM_EVENTS`.

Also walks `$AXIOM_HOME/hooks/<event>.py` (default ``~/.axiom/hooks/``) and
`./.axiom/hooks/<event>.py` (project-local) for filesystem-drop hooks.

See ``docs/specs/spec-hooks.md`` §7.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.extensions.contracts import Extension
from axiom.infra.hooks.event_schemas import (
    INTERCEPTOR_EVENTS,
    OBSERVER_EVENTS,
    PLATFORM_EVENTS,
)
from axiom.infra.hooks.hookbus import HookBus
from axiom.infra.hooks.types import HookSpec

log = logging.getLogger("axiom.infra.hooks.registry")


# Tuple form of an observer registration:
#   (event_name, subscribe_pattern, callable, fail_mode, source)
ObserverRegistration = tuple[str, str, Any, str, str]


@dataclass
class RegistrationSummary:
    """Outcome of a discovery pass — used for the startup log line."""

    interceptors: int = 0
    observers: int = 0
    user_hooks: int = 0
    project_hooks: int = 0
    skipped_unknown: list[str] = field(default_factory=list)
    skipped_import_error: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        total = self.interceptors + self.observers
        return (
            f"Registered {total} hooks "
            f"({self.interceptors} interceptors, {self.observers} observers); "
            f"{self.user_hooks} user-level, {self.project_hooks} project-local"
        )


# ---------------------------------------------------------------------------
# Entry-point resolution
# ---------------------------------------------------------------------------


def _resolve_entry(entry: str) -> Any:
    """Resolve a ``"module.path:symbol"`` entry to a callable.

    Raises:
        ImportError: module cannot be imported.
        AttributeError: module has no such symbol.
    """
    if ":" not in entry:
        raise ValueError(f"hook entry must be 'module:symbol', got {entry!r}")
    module_path, symbol = entry.split(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, symbol)


# ---------------------------------------------------------------------------
# Manifest discovery
# ---------------------------------------------------------------------------


def discover_manifest_hooks(
    extensions: Iterable[Extension],
) -> tuple[list[HookSpec], list[ObserverRegistration]]:
    """Walk extensions and split their hook declarations into routing buckets.

    Returns:
        ``(interceptor_specs, observer_registrations)``. Observer
        registrations are tuples carrying the data needed by the caller
        to feed `EventBus.subscribe`.
    """
    interceptors: list[HookSpec] = []
    observers: list[ObserverRegistration] = []

    for ext in extensions:
        for hook_def in ext.hooks:
            try:
                fn = _resolve_entry(hook_def.entry)
            except (ImportError, AttributeError, ValueError) as exc:
                log.warning(
                    "skipping hook %s in extension %s: %s",
                    hook_def.entry,
                    ext.name,
                    exc,
                )
                continue

            for event in hook_def.events:
                if event in INTERCEPTOR_EVENTS:
                    interceptors.append(
                        HookSpec(
                            event=event,
                            entry=fn,
                            priority=hook_def.priority,
                            fail_mode=hook_def.fail_mode,  # type: ignore[arg-type]
                            source=ext.name,
                        ),
                    )
                elif event in OBSERVER_EVENTS:
                    observers.append(
                        (event, event, fn, hook_def.fail_mode, ext.name),
                    )
                else:
                    # Extension-namespaced events (e.g., `tidy.pressure_critical`,
                    # `chat.session.started`) are not in the platform's closed
                    # taxonomy but are valid bus subjects — extensions own
                    # their own namespaces per spec §4 "Platform vs. extension
                    # event ownership". Treat them as observer subscriptions
                    # by default, including pattern-form (`cli.*`, `doctor.*`)
                    # which the EventBus matcher handles directly.
                    observers.append(
                        (event, event, fn, hook_def.fail_mode, ext.name),
                    )

    return interceptors, observers


# ---------------------------------------------------------------------------
# Filesystem-drop discovery
# ---------------------------------------------------------------------------


def _load_hook_module(file_path: Path, mod_name: str) -> Any:
    """Load a single hook module from a filesystem path."""
    spec = importlib.util.spec_from_file_location(mod_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load hook module from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def discover_filesystem_hooks(
    *,
    user_dir: Path | None,
    project_dir: Path | None,
    trust_project: bool,
) -> tuple[list[HookSpec], list[ObserverRegistration]]:
    """Walk ``$AXIOM_HOME/hooks/`` and ``./.axiom/hooks/`` for hook files.

    Each ``<event>.py`` may export ``def hook(ctx)`` (interceptor) and/or
    ``def observer(subject, data)`` (observer). The function name
    disambiguates routing.

    Args:
        user_dir: Path to user-level hooks (e.g., ``~/.axiom/hooks/``).
        project_dir: Path to project-local hooks (e.g., ``.axiom/hooks/``).
            Honored only when ``trust_project`` is True.
        trust_project: Caller-supplied trust gate for project hooks. Per
            spec §10, project-local hooks require an extra confirmation;
            the caller is responsible for the trust prompt.
    """
    interceptors: list[HookSpec] = []
    observers: list[ObserverRegistration] = []

    sources: list[tuple[Path, str]] = []
    if user_dir is not None and user_dir.is_dir():
        sources.append((user_dir, "user"))
    if project_dir is not None and project_dir.is_dir() and trust_project:
        sources.append((project_dir, "project"))

    for hook_dir, source in sources:
        for py_file in sorted(hook_dir.glob("*.py")):
            event = py_file.stem
            if event not in PLATFORM_EVENTS:
                log.warning(
                    "filesystem hook %s targets unknown event %r; skipping",
                    py_file,
                    event,
                )
                continue
            mod_name = f"axiom_hook_{source}_{event.replace('.', '_')}"
            try:
                module = _load_hook_module(py_file, mod_name)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to load hook %s: %s", py_file, exc)
                continue

            interceptor_fn = getattr(module, "hook", None)
            observer_fn = getattr(module, "observer", None)

            if interceptor_fn is not None and event in INTERCEPTOR_EVENTS:
                interceptors.append(
                    HookSpec(
                        event=event,
                        entry=interceptor_fn,
                        priority=100,
                        fail_mode="warn",  # filesystem drops default to warn
                        source=source,
                    ),
                )
            elif interceptor_fn is not None and event in OBSERVER_EVENTS:
                # Observer event with `def hook` — filesystem author
                # mismatched the function name to the event tier.
                log.warning(
                    "filesystem hook %s defines `hook(ctx)` but event %s"
                    " is observer-only; rename to `observer(subject, data)`",
                    py_file,
                    event,
                )

            if observer_fn is not None and event in OBSERVER_EVENTS:
                observers.append((event, event, observer_fn, "warn", source))
            elif observer_fn is not None and event in INTERCEPTOR_EVENTS:
                log.warning(
                    "filesystem hook %s defines `observer(subject, data)`"
                    " but event %s is interceptor-only; rename to"
                    " `hook(ctx)`",
                    py_file,
                    event,
                )

    return interceptors, observers


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------


def _user_hook_dir() -> Path:
    """Resolve ``$AXIOM_HOME/hooks/`` (default ``~/.axiom/hooks/``)."""
    home = os.environ.get("AXIOM_HOME", "")
    if home:
        return Path(home) / "hooks"
    return Path.home() / ".axiom" / "hooks"


def _project_hook_dir() -> Path | None:
    """Locate ``./.axiom/hooks/`` walking up from cwd."""
    cwd = Path.cwd().resolve()
    for candidate in [cwd, *cwd.parents]:
        d = candidate / ".axiom" / "hooks"
        if d.is_dir():
            return d
    return None


# ---------------------------------------------------------------------------
# HookRegistry — orchestrates the two discovery passes and wiring.
# ---------------------------------------------------------------------------


class HookRegistry:
    """Discovers + wires hooks at runtime startup.

    Args:
        hookbus: Target `HookBus` for interceptor events.
        eventbus: Target `EventBus` for observer events.
        trust_project: When True, project-local filesystem hooks are
            registered; when False, they are skipped (caller surfaces
            a trust prompt elsewhere). Defaults to False (safe).
    """

    def __init__(
        self,
        *,
        hookbus: HookBus,
        eventbus: Any,
        trust_project: bool = False,
    ) -> None:
        self._hookbus = hookbus
        self._eventbus = eventbus
        self._trust_project = trust_project

    # ----- registration entry points ---------------------------------------------

    def register_extensions(
        self,
        extensions: Iterable[Extension],
    ) -> RegistrationSummary:
        """Register every hook declared by ``extensions``."""
        summary = RegistrationSummary()
        interceptors, observers = discover_manifest_hooks(extensions)
        for spec in interceptors:
            self._hookbus.register(spec)
            summary.interceptors += 1
        for event, pattern, fn, fail_mode, source in observers:
            self._eventbus.subscribe(
                pattern,
                fn,
                fail_mode=fail_mode,  # type: ignore[arg-type]
                source=source,
            )
            summary.observers += 1
        return summary

    def register_filesystem(self) -> RegistrationSummary:
        """Register every filesystem-drop hook found in user / project dirs."""
        summary = RegistrationSummary()
        user_dir = _user_hook_dir()
        project_dir = _project_hook_dir()
        interceptors, observers = discover_filesystem_hooks(
            user_dir=user_dir,
            project_dir=project_dir,
            trust_project=self._trust_project,
        )
        for spec in interceptors:
            self._hookbus.register(spec)
            summary.interceptors += 1
            if spec.source == "user":
                summary.user_hooks += 1
            elif spec.source == "project":
                summary.project_hooks += 1
        for event, pattern, fn, fail_mode, source in observers:
            self._eventbus.subscribe(
                pattern,
                fn,
                fail_mode=fail_mode,  # type: ignore[arg-type]
                source=source,
            )
            summary.observers += 1
            if source == "user":
                summary.user_hooks += 1
            elif source == "project":
                summary.project_hooks += 1
        return summary


__all__ = [
    "HookRegistry",
    "ObserverRegistration",
    "RegistrationSummary",
    "discover_filesystem_hooks",
    "discover_manifest_hooks",
]
