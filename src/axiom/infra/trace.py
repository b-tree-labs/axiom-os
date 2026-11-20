# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Trace context propagation.

Every logical operation (one LLM request, one signal ingest run, one TIDY task)
gets a trace_id that flows through every log call in that thread/coroutine via
Python contextvars. No manual threading of IDs through function arguments.

Usage:
    from axiom.infra.trace import new_trace, current_trace, set_session, current_session

    # At the start of an operation:
    trace_id = new_trace()

    # Anywhere in the call stack — reads from context automatically:
    logger.info("doing work", extra={"trace_id": current_trace()})

    # StructuredJsonFormatter calls current_trace() automatically — you only
    # need new_trace() at entry points, never at individual log call sites.
"""

from __future__ import annotations

import contextvars
import uuid

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "neut_trace_id", default=""
)
_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "neut_session_id", default=""
)


def new_trace() -> str:
    """Start a new trace for the current context. Returns the new trace_id."""
    tid = uuid.uuid4().hex[:16]
    _trace_id.set(tid)
    return tid


def current_trace() -> str:
    """Return the active trace_id, or a stable 'no-trace' sentinel if none set."""
    return _trace_id.get() or "no-trace"


def set_session(sid: str) -> None:
    """Set the session_id for the current context."""
    _session_id.set(sid)


def current_session() -> str:
    """Return the active session_id, or 'anonymous' if none set."""
    return _session_id.get() or "anonymous"
