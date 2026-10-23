# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""get_trace_provider factory — selects backend from config."""

from __future__ import annotations


def test_factory_returns_null_by_default() -> None:
    from axiom.infra.tracing import NullTraceProvider, get_trace_provider

    p = get_trace_provider({})
    assert isinstance(p, NullTraceProvider)


def test_factory_selects_in_memory() -> None:
    from axiom.infra.tracing import InMemoryTraceProvider, get_trace_provider

    p = get_trace_provider({"backend": "in_memory"})
    assert isinstance(p, InMemoryTraceProvider)


def test_factory_selects_null_explicit() -> None:
    from axiom.infra.tracing import NullTraceProvider, get_trace_provider

    p = get_trace_provider({"backend": "null"})
    assert isinstance(p, NullTraceProvider)


def test_factory_raises_on_unknown_backend() -> None:
    import pytest

    from axiom.infra.tracing import get_trace_provider

    with pytest.raises(ValueError, match="unknown trace backend"):
        get_trace_provider({"backend": "nonsense"})
