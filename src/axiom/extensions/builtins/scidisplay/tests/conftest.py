# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the Sci Displays test suite.

The chart-spec kind and window-basis registries are process-global and open by
design, so a test that registers an entry would otherwise leak it into every
later test in the session. This snapshots both registries and restores them.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay import chart_spec


def _snapshot() -> tuple[dict[str, object], dict[str, object]]:
    kinds = {name: chart_spec.lookup_kind(name) for name in chart_spec.registered_kinds()}
    bases = {
        name: chart_spec.lookup_window_basis(name) for name in chart_spec.registered_window_bases()
    }
    return kinds, bases


def _restore(kinds: dict[str, object], bases: dict[str, object]) -> None:
    for name in chart_spec.registered_kinds():
        chart_spec.unregister_kind(name)
    for entry in kinds.values():
        chart_spec.register_kind(entry)  # type: ignore[arg-type]
    for name in chart_spec.registered_window_bases():
        chart_spec.unregister_window_basis(name)
    for entry in bases.values():
        chart_spec.register_window_basis(entry)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def isolate_chart_registries():
    kinds, bases = _snapshot()
    try:
        yield
    finally:
        _restore(kinds, bases)
