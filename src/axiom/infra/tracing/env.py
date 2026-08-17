# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Env-var driven tracing configuration — Prague deployment (T1a).

A node operator enables observability by setting env vars on the node;
no code edit or config-file change is required. The precedence is:

1. ``AXIOM_TRACE_BACKEND`` — explicit backend override
   (``null``, ``in_memory``, ``langfuse``). Wins over the keys-based
   auto-detection. Useful to force tracing off at a site even when
   LangFuse keys are present.
2. If BOTH ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are set,
   the LangFuse backend is selected and ``LANGFUSE_HOST`` is forwarded
   if present.
3. Otherwise the null backend is returned (no-op).

Invalid backend names silently fall through to null — a typo in a
deployment env var never takes the node down.
"""

from __future__ import annotations

import os
from typing import Any

LANGFUSE_ENV_VARS: tuple[str, ...] = (
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
)

_VALID_BACKENDS: frozenset[str] = frozenset({"null", "in_memory", "langfuse"})


def load_trace_config_from_env(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Derive a trace-config dict from ``env`` (defaults to ``os.environ``).

    Returns a dict suitable for the existing ``get_trace_provider``
    factory. Never raises.
    """
    e = env if env is not None else os.environ

    explicit = e.get("AXIOM_TRACE_BACKEND", "").strip().lower()
    if explicit:
        if explicit not in _VALID_BACKENDS:
            # Unknown backend → null (defensive default).
            return {"backend": "null"}
        if explicit == "langfuse":
            return _langfuse_config(e)
        return {"backend": explicit}

    # Auto-detect: both Langfuse keys present ⇒ Langfuse.
    public = e.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = e.get("LANGFUSE_SECRET_KEY", "").strip()
    if public and secret:
        return _langfuse_config(e)

    return {"backend": "null"}


def _langfuse_config(e: dict[str, str]) -> dict[str, Any]:
    public = e.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = e.get("LANGFUSE_SECRET_KEY", "").strip()
    # Guard: even on explicit backend=langfuse, refuse to build without
    # both keys — a LangfuseTraceProvider without creds would crash the
    # first time it tried to flush. Fall through to null silently.
    if not public or not secret:
        return {"backend": "null"}
    cfg: dict[str, Any] = {
        "backend": "langfuse",
        "public_key": public,
        "secret_key": secret,
    }
    host = e.get("LANGFUSE_HOST", "").strip()
    if host:
        cfg["host"] = host
    return cfg


def build_trace_provider_from_env(
    env: dict[str, str] | None = None,
    *,
    _transport: Any = None,
) -> Any:
    """Build a TraceProvider from the current env.

    Tests pass ``_transport`` to inject a fake HTTP transport (avoiding
    real network calls). Production callers omit it; the Langfuse
    provider constructs its own ``_HttpTransport`` from the keys.
    """
    cfg = load_trace_config_from_env(env)
    backend = cfg.get("backend", "null")

    if backend == "null":
        from axiom.infra.tracing.null_provider import NullTraceProvider

        return NullTraceProvider()

    if backend == "in_memory":
        from axiom.infra.tracing.in_memory_provider import InMemoryTraceProvider

        return InMemoryTraceProvider()

    if backend == "langfuse":
        from axiom.infra.tracing.langfuse_provider import LangfuseTraceProvider

        return LangfuseTraceProvider(
            transport=_transport,
            public_key=cfg.get("public_key"),
            secret_key=cfg.get("secret_key"),
            host=cfg.get("host"),
        )

    # Defensive default — unknown backend (shouldn't happen given the
    # load-side validation, but belt-and-suspenders).
    from axiom.infra.tracing.null_provider import NullTraceProvider

    return NullTraceProvider()
