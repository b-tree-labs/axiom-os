# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Synchronous interceptor primitive for platform lifecycle events.

`HookBus` runs registered interceptors in priority order, splices payload
modifications between hooks, and short-circuits on the first ``deny`` /
``approval_required``. Per-hook ``fail_mode`` controls how exceptions
raised by an interceptor flow.

See ``docs/specs/spec-hooks.md`` §5.
"""

from __future__ import annotations

import logging
import threading
import traceback as _traceback
from collections.abc import Iterable
from typing import Any

from axiom.infra.hooks.priority import (
    ManifestPriorityStrategy,
    PriorityStrategy,
)
from axiom.infra.hooks.types import (
    HookContext,
    HookResult,
    HookSpec,
    allow,
)

log = logging.getLogger("axiom.infra.hooks")


def _qualname(obj: Any) -> str:
    mod = getattr(obj, "__module__", "")
    name = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", repr(obj))
    return f"{mod}.{name}" if mod else name


class HookBus:
    """In-process interceptor dispatch.

    Args:
        strategy: Priority ordering strategy. Defaults to
            ``ManifestPriorityStrategy``.
        event_bus: Optional `EventBus` to publish ``bus.errors`` events
            when a hook raises (carrying forward the bus v2.0 contract).
            None disables bus.errors integration; tests may pass a fresh
            ``EventBus()`` to assert on the topic.
    """

    def __init__(
        self,
        strategy: PriorityStrategy | None = None,
        *,
        event_bus: Any | None = None,
    ) -> None:
        self._strategy: PriorityStrategy = strategy or ManifestPriorityStrategy()
        self._event_bus = event_bus
        # Registry mutex — covers both reads and writes. Per spec §10.5
        # invariant 1, registry mutation is atomic; per invariant 2, fire
        # captures a snapshot under the lock then iterates without it.
        self._lock = threading.RLock()
        self._hooks: dict[str, list[HookSpec]] = {}
        # Recursion guard mirrors EventBus: hooks that fail while handling
        # a bus.errors-related context get demoted to "ignore".
        self._demoted: set[int] = set()

    # ----- registry management ----------------------------------------------------

    def register(self, spec: HookSpec) -> None:
        """Register an interceptor for ``spec.event``."""
        with self._lock:
            self._hooks.setdefault(spec.event, []).append(spec)

    def unregister(self, spec: HookSpec) -> None:
        """Remove a previously registered interceptor."""
        with self._lock:
            bucket = self._hooks.get(spec.event)
            if not bucket:
                return
            self._hooks[spec.event] = [s for s in bucket if s is not spec]

    def hooks_for(self, event: str) -> list[HookSpec]:
        """Diagnostic: return ordered hooks registered for ``event``."""
        with self._lock:
            snapshot = list(self._hooks.get(event, ()))
        return self._strategy.order(snapshot)

    # ----- fire ------------------------------------------------------------------

    def fire(
        self,
        event: str,
        payload: dict[str, Any],
        principal: str,
    ) -> HookResult:
        """Run every interceptor for ``event`` and aggregate the result.

        Snapshot-then-iterate: ordering is captured under the registry
        lock, then released before any interceptor runs (per spec §10.5
        invariant 2). New registrations during iteration affect the next
        fire, never this one.

        Returns:
            Aggregated `HookResult`. ``allow`` if every interceptor
            returned allow. ``modify`` with the union of modifications
            if any returned ``allow_modified``. The first ``deny`` /
            ``approval_required`` short-circuits and is returned as-is.
        """
        with self._lock:
            snapshot = list(self._hooks.get(event, ()))
        ordered = self._strategy.order(snapshot)

        # Working copy of the payload — modifications splice in here so
        # subsequent hooks see the modified version.
        current_payload: dict[str, Any] = dict(payload)
        accumulated_modifications: dict[str, Any] = {}

        for spec in ordered:
            ctx = HookContext(
                event=event,
                payload=current_payload,
                principal=principal,
            )
            try:
                result = spec.entry(ctx)
            except Exception as exc:
                self._handle_handler_error(spec, ctx, exc)
                # Per fail_mode: abort already re-raised; warn/ignore
                # treat as allow and continue.
                if id(spec) in self._demoted:
                    continue
                if spec.fail_mode == "abort":
                    raise  # pragma: no cover - re-raised by _handle_handler_error
                continue

            if result is None:
                # Forgiving default: a hook that returned nothing is allow().
                result = allow()

            if result.decision == "deny":
                return result
            if result.decision == "approval_required":
                return result
            if result.decision == "modify":
                if result.modified_payload:
                    current_payload.update(result.modified_payload)
                    accumulated_modifications.update(result.modified_payload)
                continue
            # allow — fall through to next hook

        if accumulated_modifications:
            return HookResult(
                decision="modify",
                modified_payload=accumulated_modifications,
            )
        return allow()

    # ----- error routing ----------------------------------------------------------

    def _handle_handler_error(
        self,
        spec: HookSpec,
        ctx: HookContext,
        exc: BaseException,
    ) -> None:
        """Apply the hook's ``fail_mode`` and route the failure.

        For ``warn`` and ``ignore``, also publishes a ``bus.errors`` event
        when an `EventBus` was wired at construction. ``abort`` re-raises
        after publishing.
        """
        # Publish to bus.errors when wired. Recursion guard: a hook that
        # fails while we are already handling errors gets demoted.
        if self._event_bus is not None and id(spec) not in self._demoted:
            try:
                self._event_bus.publish(
                    "bus.errors",
                    {
                        "handler": _qualname(spec.entry),
                        "original_subject": ctx.event,
                        "original_payload": ctx.payload,
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                        "traceback": "".join(
                            _traceback.format_exception(
                                type(exc), exc, exc.__traceback__,
                            ),
                        ),
                        "fail_mode": spec.fail_mode,
                        "source": spec.source,
                    },
                    source="hookbus",
                )
            except Exception as publish_exc:
                # Demote the hook to break any recursive bus.errors loop
                # and surface a single warning. We never let bus.errors
                # publishing failures shadow the original exception.
                self._demoted.add(id(spec))
                log.warning(
                    "hook %s failed and bus.errors publish also failed (%s);"
                    " demoting hook to ignore",
                    _qualname(spec.entry),
                    publish_exc,
                )

        if spec.fail_mode == "abort":
            log.error(
                "hook %s raised on %s: %s — aborting (fail_mode=abort)",
                _qualname(spec.entry),
                ctx.event,
                exc,
            )
            raise exc
        elif spec.fail_mode == "warn":
            log.warning(
                "hook %s raised on %s: %s",
                _qualname(spec.entry),
                ctx.event,
                exc,
            )
        # ignore — silent (bus.errors event is the only signal)

    # ----- bulk reset -------------------------------------------------------------

    def clear(self) -> None:
        """Drop every registered hook. Test-only convenience."""
        with self._lock:
            self._hooks.clear()
            self._demoted.clear()

    def all_hooks(self) -> Iterable[HookSpec]:
        """Diagnostic: every registered hook (unordered)."""
        with self._lock:
            return [s for bucket in self._hooks.values() for s in bucket]


# ---------------------------------------------------------------------------
# Process-wide default — hooks register against this unless an explicit
# bus is injected (which production should). Keeps test wiring simple.
# ---------------------------------------------------------------------------


_default_bus: HookBus | None = None
_default_lock = threading.Lock()


def get_default_hookbus() -> HookBus:
    """Return the lazily-instantiated process default `HookBus`.

    Production wiring should pass a hook bus explicitly to call sites; the
    default exists so user-level hook drops and tests work without
    additional plumbing.
    """
    global _default_bus
    with _default_lock:
        if _default_bus is None:
            _default_bus = HookBus()
        return _default_bus


def set_default_hookbus(bus: HookBus | None) -> None:
    """Replace the process default. Test-only — production should not call."""
    global _default_bus
    with _default_lock:
        _default_bus = bus


__all__ = [
    "HookBus",
    "get_default_hookbus",
    "set_default_hookbus",
]
