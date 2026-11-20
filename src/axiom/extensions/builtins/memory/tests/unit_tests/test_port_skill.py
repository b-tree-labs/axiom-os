# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``memory.port`` — the one-command cross-harness onboarding skill.

``port`` is a pure composition of already-tested primitives (mcp install,
absorb, import, reindex), so these tests stub the callees at their source
modules and assert the orchestration contract: which steps run, with which
params, and how per-step failure aggregates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from axiom.infra.skills import SkillResult

PRINCIPAL = "@alice:personal"


class _Calls:
    """Record of every stubbed callee invocation, in order."""

    def __init__(self) -> None:
        self.mcp: list[dict[str, Any]] = []
        self.absorb: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        self.reindex: list[dict[str, Any]] = []


@pytest.fixture
def calls(monkeypatch) -> _Calls:
    """Stub the four callees; every stub succeeds unless a test overrides."""
    rec = _Calls()

    def fake_install(**kwargs):
        rec.mcp.append(kwargs)
        return {
            "server": "axiom",
            "dry_run": kwargs.get("dry_run", False),
            "detected": {"claude-code": True},
            "ingress": {"action": "skipped"},
            "results": {"claude-code": {"action": "added"}},
        }

    def fake_absorb(params, ctx):
        rec.absorb.append(params)
        return SkillResult(value={
            "harness": params["harness"], "principal": params["principal"],
            "dry_run": bool(params.get("dry_run")), "candidates": 3,
            "imported": 2, "skipped_echo": 1, "collapsed_exact": 0,
            "merged_near_dup": 0, "conflicts_queued": 0, "skipped": [],
        })

    def fake_import(params, ctx):
        rec.imports.append(params)
        return SkillResult(value={
            "assume_principal": params["assume_principal"],
            "from_principal": "@alice:old", "imported": 5,
            "skipped_duplicate": 0, "conflicts": [], "sessions_imported": 2,
        })

    def fake_reindex(params, ctx):
        rec.reindex.append(params)
        return SkillResult(value={"principals": [params.get("principal")]})

    from importlib import import_module

    # import_module (not ``import ... as``): the skills package re-exports
    # same-named functions, which shadow the submodules on attribute access.
    mcp_install = import_module("axiom.extensions.builtins.mcp.install")
    absorb_mod = import_module("axiom.extensions.builtins.memory.skills.absorb")
    import_mod = import_module(
        "axiom.extensions.builtins.memory.skills.import_bundle"
    )
    reindex_mod = import_module(
        "axiom.extensions.builtins.memory.skills.reindex_recall"
    )

    monkeypatch.setattr(mcp_install, "install", fake_install)
    monkeypatch.setattr(absorb_mod, "absorb", fake_absorb)
    monkeypatch.setattr(import_mod, "import_bundle", fake_import)
    monkeypatch.setattr(reindex_mod, "reindex_recall", fake_reindex)
    return rec


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A fake $HOME with both harness stores present."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    return tmp_path


def _port(params: dict[str, Any]) -> SkillResult:
    from axiom.extensions.builtins.memory.skills.port import port

    return port(params, None)


def _base(home: Path) -> dict[str, Any]:
    return {"composition": object(), "principal": PRINCIPAL, "home": str(home)}


# ---------------------------------------------------------------------------
# Orchestration: initial port
# ---------------------------------------------------------------------------


class TestInitialPort:
    def test_composes_mcp_then_absorb_per_detected_harness(self, calls, home):
        result = _port(_base(home))
        assert result.ok
        assert len(calls.mcp) == 1
        absorbed = {p["harness"] for p in calls.absorb}
        assert absorbed == {"claude-code", "codex"}
        assert set(result.value["harnesses"]) == {"claude-code", "codex"}
        assert result.value["mcp"]["results"]["claude-code"]["action"] == "added"
        assert result.value["bundle"] is None
        assert result.value["reindex"] is None

    def test_absorb_receives_principal_and_passthrough_params(self, calls, home):
        params = _base(home) | {"account": "work", "roots": ["/repo/a"]}
        _port(params)
        for p in calls.absorb:
            assert p["principal"] == PRINCIPAL
            assert p["account"] == "work"
            assert p["roots"] == ["/repo/a"]
            assert p["home"] == str(home)

    def test_detects_only_present_harnesses(self, calls, tmp_path):
        (tmp_path / ".claude").mkdir()
        result = _port(_base(tmp_path))
        assert result.ok
        assert [p["harness"] for p in calls.absorb] == ["claude-code"]

    def test_no_harness_store_is_still_a_valid_port(self, calls, tmp_path):
        result = _port(_base(tmp_path))
        assert result.ok
        assert calls.absorb == []
        assert result.value["harnesses"] == {}
        assert len(calls.mcp) == 1

    def test_explicit_harness_list_overrides_detection(self, calls, tmp_path):
        result = _port(_base(tmp_path) | {"harnesses": ["codex"]})
        assert result.ok
        assert [p["harness"] for p in calls.absorb] == ["codex"]


# ---------------------------------------------------------------------------
# Bundle import (machine move)
# ---------------------------------------------------------------------------


class TestBundleImport:
    def test_bundle_triggers_import_assumed_as_principal(self, calls, home):
        result = _port(_base(home) | {
            "bundle": "/x/bundle.tar.gz", "sessions_dir": "/x/sessions",
        })
        assert result.ok
        assert len(calls.imports) == 1
        p = calls.imports[0]
        assert p["bundle"] == "/x/bundle.tar.gz"
        assert p["assume_principal"] == PRINCIPAL
        assert p["sessions_dir"] == "/x/sessions"
        assert p["home"] == str(home)  # store restore lands in the same home
        assert result.value["bundle"]["imported"] == 5

    def test_no_bundle_no_import(self, calls, home):
        _port(_base(home))
        assert calls.imports == []


# ---------------------------------------------------------------------------
# Refresh mode
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_refresh_reindexes_recall_corpus(self, calls, home):
        result = _port(_base(home) | {"refresh": True})
        assert result.ok
        assert len(calls.reindex) == 1
        assert calls.reindex[0]["principal"] == PRINCIPAL
        assert result.value["reindex"] is not None

    def test_initial_port_does_not_reindex(self, calls, home):
        _port(_base(home))
        assert calls.reindex == []


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_propagates_to_every_step(self, calls, home):
        result = _port(_base(home) | {
            "bundle": "/x/b.tar.gz", "refresh": True, "dry_run": True,
        })
        assert result.ok
        assert calls.mcp[0]["dry_run"] is True
        assert all(p["dry_run"] for p in calls.absorb)
        assert calls.imports[0]["dry_run"] is True
        # Reindex is a repair write with no meaningful dry form — skipped.
        assert calls.reindex == []
        assert result.value["reindex"] == {"action": "skipped-dry-run"}


# ---------------------------------------------------------------------------
# Failure aggregation
# ---------------------------------------------------------------------------


class TestFailures:
    def test_one_harness_failing_does_not_stop_the_other(
        self, calls, home, monkeypatch,
    ):
        from importlib import import_module

        absorb_mod = import_module(
            "axiom.extensions.builtins.memory.skills.absorb"
        )
        real_fake = absorb_mod.absorb

        def failing_claude(params, ctx):
            if params["harness"] == "claude-code":
                calls.absorb.append(params)
                return SkillResult(ok=False, errors=["store unreadable"])
            return real_fake(params, ctx)

        monkeypatch.setattr(absorb_mod, "absorb", failing_claude)
        result = _port(_base(home))
        assert not result.ok
        assert any("claude-code" in e for e in result.errors)
        absorbed = {p["harness"] for p in calls.absorb}
        assert absorbed == {"claude-code", "codex"}
        assert result.value["harnesses"]["codex"]["imported"] == 2

    def test_mcp_install_error_is_captured_and_absorb_still_runs(
        self, calls, home, monkeypatch,
    ):
        from importlib import import_module

        mcp_install = import_module("axiom.extensions.builtins.mcp.install")

        def boom(**kwargs):
            raise RuntimeError("no writable config")

        monkeypatch.setattr(mcp_install, "install", boom)
        result = _port(_base(home))
        assert not result.ok
        assert any("no writable config" in e for e in result.errors)
        assert result.value["mcp"]["error"]
        assert len(calls.absorb) == 2

    def test_missing_required_params(self, calls, home):
        assert not _port({"principal": PRINCIPAL}).ok
        assert not _port({"composition": object()}).ok
