# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Core types for the platform hooks primitive.

`HookContext` and `HookResult` shape the interceptor contract; the
decision factories (`allow`, `allow_modified`, `deny`, `request_approval`)
keep authoring concise.

See ``docs/specs/spec-hooks.md`` §5.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

# Per-hook failure mode. `abort` re-raises, `warn` logs and continues,
# `ignore` swallows silently. Mirrors EventBus FailMode.
FailMode = Literal["abort", "warn", "ignore"]


@dataclass(frozen=True)
class HookContext:
    """Immutable context passed to every interceptor hook.

    Attributes:
        event: Subject of the lifecycle event (e.g., ``"tool.pre_invoke"``).
        payload: Event-specific dict. Modifications by previous hooks are
            spliced in by `HookBus` before this hook is called.
        principal: Caller principal in ``@name:context`` form.
        cancellation_reason: Populated by upstream `deny()` chain when
            re-fired downstream; empty for the first hook.
    """

    event: str
    payload: dict[str, Any]
    principal: str
    cancellation_reason: str = ""


@dataclass(frozen=True)
class HookResult:
    """Decision returned by an interceptor hook.

    Attributes:
        decision: One of ``allow``, ``modify``, ``deny``, ``approval_required``.
        modified_payload: When ``decision == "modify"``, the keys to splice
            into the event payload before the next hook runs.
        reason: Human-readable explanation for ``deny`` / ``approval_required``.
        approval_token: Reserved for future RACI integration; empty in v1.
    """

    decision: Literal["allow", "modify", "deny", "approval_required"]
    modified_payload: dict[str, Any] | None = None
    reason: str = ""
    approval_token: str = ""


# Interceptor entry signature.
HookEntry = Callable[[HookContext], HookResult]


@dataclass(frozen=True)
class HookSpec:
    """Discovered interceptor registration.

    Attributes:
        event: Lifecycle event subject this hook intercepts.
        entry: Callable returning a `HookResult`.
        priority: Lower runs first. AEOS default is 100.
        fail_mode: How to treat exceptions raised by `entry`.
        source: Origin label — extension name, ``"user"``, ``"project"``,
            or ``"platform"``.
    """

    event: str
    entry: HookEntry
    priority: int = 100
    fail_mode: FailMode = "abort"
    source: str = ""

    def __post_init__(self) -> None:  # pragma: no cover - simple guard
        # Validate fail_mode at construction time so misconfigured manifests
        # surface early rather than at fire-time.
        if self.fail_mode not in ("abort", "warn", "ignore"):
            raise ValueError(
                f"invalid fail_mode {self.fail_mode!r}; must be one of "
                "'abort', 'warn', 'ignore'",
            )


# ---------------------------------------------------------------------------
# Decision factories — keep authoring code concise.
# ---------------------------------------------------------------------------


def allow() -> HookResult:
    """Pass through unchanged."""
    return HookResult(decision="allow")


def allow_modified(**modifications: Any) -> HookResult:
    """Pass through after splicing the given keys into the event payload."""
    if not modifications:
        # Allow `allow_modified()` with no kwargs as an explicit no-op
        # marker, but the standard form is `allow()`.
        return HookResult(decision="modify", modified_payload={})
    return HookResult(decision="modify", modified_payload=dict(modifications))


def deny(*, reason: str) -> HookResult:
    """Block the operation. The reason surfaces to the caller."""
    return HookResult(decision="deny", reason=reason)


def request_approval(*, why: str, approval_token: str = "") -> HookResult:
    """Pause the operation pending a human signal. v1: surfaces as an error."""
    return HookResult(decision="approval_required", reason=why, approval_token=approval_token)


# ---------------------------------------------------------------------------
# Exceptions raised at call-sites when a hook decision aborts the operation.
# ---------------------------------------------------------------------------


class HookDenied(Exception):
    """Raised at a call-site when a hook returned `deny()`.

    `result.reason` is preserved as the exception message so users see a
    clean explanation rather than a stack trace.
    """

    def __init__(self, reason: str, *, hook_source: str = "") -> None:
        self.reason = reason
        self.hook_source = hook_source
        msg = f"denied by hook ({hook_source}): {reason}" if hook_source else f"denied by hook: {reason}"
        super().__init__(msg)


class ApprovalRequired(Exception):
    """Raised at a call-site when a hook returned `request_approval()`."""

    def __init__(self, reason: str, *, hook_source: str = "", token: str = "") -> None:
        self.reason = reason
        self.hook_source = hook_source
        self.token = token
        msg = (
            f"approval required ({hook_source}): {reason}"
            if hook_source
            else f"approval required: {reason}"
        )
        super().__init__(msg)


# Compatibility re-export — non-frozen field default factory pattern in case
# downstream callers do `from axiom.infra.hooks.types import field` (none today).
__all__ = [
    "ApprovalRequired",
    "FailMode",
    "HookContext",
    "HookDenied",
    "HookEntry",
    "HookResult",
    "HookSpec",
    "allow",
    "allow_modified",
    "deny",
    "field",
    "request_approval",
]
