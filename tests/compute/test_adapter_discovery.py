# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.compute.adapters entry-point discovery.

Closes the gap that made `pip install axiom-ext-openmc; axi model run --on local:openmc`
fail with "unknown kernel 'openmc'": the adapter self-registered in its module's
import-time side-effect, but `import axiom_ext_openmc` never imported the adapter
(the package's __init__ is intentionally minimal so reference-seeding consumers
don't drag in axiom.compute).

Entry points solve this declaratively — the package's pyproject.toml declares the
adapter under `[project.entry-points."axiom.compute.adapters"]` and importlib.metadata
discovers it without anyone importing anything.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from axiom.compute.adapters import (
    CodeAdapter,
    KernelResult,
    get_adapter,
    register_adapter,
)
from axiom.compute.adapters import _REGISTRY  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Static behavior — preserved from before
# ---------------------------------------------------------------------------


def test_static_mock_adapter_always_available():
    assert "mock" in _REGISTRY
    adapter = get_adapter("mock")
    assert adapter is not None


def test_register_adapter_adds_to_registry():
    class DummyAdapter(CodeAdapter):
        name = "dummy_static"
        def execute(self, state, kernel_options): return KernelResult(value_summary={})  # type: ignore[arg-type]
    register_adapter("dummy_static", DummyAdapter())
    try:
        assert get_adapter("dummy_static").name == "dummy_static"
    finally:
        _REGISTRY.pop("dummy_static", None)


# ---------------------------------------------------------------------------
# Entry-point discovery
# ---------------------------------------------------------------------------


class _FakeEntryPoint:
    """Stand-in for importlib.metadata.EntryPoint with a controllable load()."""

    def __init__(self, name: str, target):
        self.name = name
        self._target = target

    def load(self):
        return self._target


class _DummyAdapter(CodeAdapter):
    name = "ep_dummy"
    def execute(self, state, kernel_options): return KernelResult(value_summary={})  # type: ignore[arg-type]


def _reset_discovery_flag():
    """Reset the module-level discovery-cache flag so a test sees a fresh scan."""
    import axiom.compute.adapters as adapters_mod
    adapters_mod._ENTRY_POINTS_LOADED = False


def test_entry_point_discovery_registers_adapter_on_miss():
    """A miss on a name that has an entry point loads + registers the adapter."""
    _reset_discovery_flag()
    _REGISTRY.pop("ep_dummy", None)

    fake_ep = _FakeEntryPoint(name="ep_dummy", target=_DummyAdapter)

    def fake_entry_points(*args, **kwargs):
        return [fake_ep]

    with patch("importlib.metadata.entry_points", fake_entry_points):
        adapter = get_adapter("ep_dummy")
        assert adapter.name == "ep_dummy"

    _REGISTRY.pop("ep_dummy", None)


def test_entry_point_discovery_accepts_instance_directly():
    """An entry point that loads to an already-instantiated adapter is also accepted."""
    _reset_discovery_flag()
    _REGISTRY.pop("ep_instance", None)

    instance = _DummyAdapter()
    instance.name = "ep_instance"  # type: ignore[misc]
    fake_ep = _FakeEntryPoint(name="ep_instance", target=instance)

    with patch("importlib.metadata.entry_points", lambda *a, **k: [fake_ep]):
        adapter = get_adapter("ep_instance")
        assert adapter is instance

    _REGISTRY.pop("ep_instance", None)


def test_static_registry_takes_precedence_over_entry_points():
    """If a name is already in _REGISTRY (static), entry points do not override."""
    _reset_discovery_flag()

    class Override(CodeAdapter):
        name = "should-not-win"
        def execute(self, state, kernel_options): return KernelResult(value_summary={})  # type: ignore[arg-type]
    fake_ep = _FakeEntryPoint(name="mock", target=Override)

    with patch("importlib.metadata.entry_points", lambda *a, **k: [fake_ep]):
        adapter = get_adapter("mock")
        # mock is the static one, not the override
        assert adapter.name == "mock"


def test_broken_entry_point_does_not_poison_registry():
    """An entry point whose load() raises should be silently skipped (logged
    debug eventually); other adapters must remain reachable, and the broken
    one's name must remain unknown."""
    _reset_discovery_flag()
    _REGISTRY.pop("ep_bad", None)
    _REGISTRY.pop("ep_good", None)

    bad_ep = _FakeEntryPoint(name="ep_bad", target=_DummyAdapter)

    def bad_load():
        raise RuntimeError("simulated extension explosion")
    bad_ep.load = bad_load  # type: ignore[method-assign]

    good_ep = _FakeEntryPoint(name="ep_good", target=_DummyAdapter)

    with patch("importlib.metadata.entry_points", lambda *a, **k: [bad_ep, good_ep]):
        # Looking up the bad one raises; looking up the good one succeeds
        with pytest.raises(ValueError, match="unknown kernel"):
            get_adapter("ep_bad")
        # Good one was loaded during the same scan
        assert get_adapter("ep_good").name == "ep_dummy"

    _REGISTRY.pop("ep_good", None)


def test_unknown_kernel_lists_registered_in_error():
    """Error message lists what IS registered so users can see what's available."""
    _reset_discovery_flag()
    with patch("importlib.metadata.entry_points", lambda *a, **k: []):
        with pytest.raises(ValueError) as excinfo:
            get_adapter("nonexistent")
        assert "nonexistent" in str(excinfo.value)
        assert "mock" in str(excinfo.value)


def test_discovery_only_runs_once_per_process():
    """The entry-points scan caches; subsequent get_adapter calls don't re-scan."""
    _reset_discovery_flag()
    call_count = {"n": 0}

    def counting_entry_points(*args, **kwargs):
        call_count["n"] += 1
        return []

    with patch("importlib.metadata.entry_points", counting_entry_points):
        # Three misses; only ONE underlying entry-points scan
        with pytest.raises(ValueError):
            get_adapter("nope1")
        with pytest.raises(ValueError):
            get_adapter("nope2")
        with pytest.raises(ValueError):
            get_adapter("nope3")
        assert call_count["n"] == 1
