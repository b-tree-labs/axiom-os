# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for :class:`SkillExecutor` — the production ``Executor``.

The PULSE-1 audit found the engine's ``Executor`` was protocol-only:
tests drove doubles and nothing in production mapped a stored cadence's
action string onto a real skill. ``SkillExecutor`` is that mapping —
action string → :meth:`SkillRegistry.invoke` with the envelope's params.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.schedule.executor import (
    SkillDispatchError,
    SkillExecutor,
)
from axiom.infra.skills import SkillRegistry, SkillResult


def _registry_with(name: str, fn) -> SkillRegistry:
    reg = SkillRegistry()
    reg.register(name, fn)
    return reg


def _executor(reg: SkillRegistry, tmp_path: Path) -> SkillExecutor:
    return SkillExecutor(reg, state_dir=tmp_path, logger=logging.getLogger("test.executor"))


class TestDispatch:
    def test_invokes_skill_with_envelope_params(self, tmp_path):
        seen: list[dict] = []

        def skill(params, ctx):
            seen.append(params)
            return SkillResult(ok=True, value={"receipt": "rcpt-1"})

        ex = _executor(_registry_with("data.backup", skill), tmp_path)
        receipt = ex.run("data.backup", {"action": "data.backup", "params": {"label": "nightly"}})
        assert seen == [{"label": "nightly"}]
        assert receipt == "rcpt-1"

    def test_missing_params_defaults_to_empty(self, tmp_path):
        seen: list[dict] = []

        def skill(params, ctx):
            seen.append(params)
            return SkillResult(ok=True)

        ex = _executor(_registry_with("data.backup", skill), tmp_path)
        ex.run("data.backup", None)
        assert seen == [{}]

    def test_ctx_carries_state_dir(self, tmp_path):
        captured = {}

        def skill(params, ctx):
            captured["state_dir"] = ctx.state_dir
            return SkillResult(ok=True)

        ex = _executor(_registry_with("data.backup", skill), tmp_path)
        ex.run("data.backup", {})
        assert captured["state_dir"] == tmp_path


class TestFailurePropagation:
    """A failed SkillResult must RAISE so the engine's retry /
    dead-letter machinery engages — swallowing it would mark the fire
    'success' and defeat the alerting chain."""

    def test_not_ok_raises(self, tmp_path):
        def skill(params, ctx):
            return SkillResult(ok=False, errors=["disk full"])

        ex = _executor(_registry_with("data.backup", skill), tmp_path)
        with pytest.raises(SkillDispatchError, match="disk full"):
            ex.run("data.backup", {})

    def test_unknown_action_raises(self, tmp_path):
        ex = _executor(SkillRegistry(), tmp_path)
        with pytest.raises(SkillDispatchError, match="no skill registered"):
            ex.run("data.nope", {})

    def test_skill_exception_raises(self, tmp_path):
        """SkillRegistry.invoke wraps exceptions as ok=False — the
        executor must still surface them as a dispatch failure."""

        def skill(params, ctx):
            raise RuntimeError("boom")

        ex = _executor(_registry_with("data.backup", skill), tmp_path)
        with pytest.raises(SkillDispatchError, match="boom"):
            ex.run("data.backup", {})


class TestReceipt:
    def test_no_receipt_returns_none(self, tmp_path):
        def skill(params, ctx):
            return SkillResult(ok=True, value={"anything": "else"})

        ex = _executor(_registry_with("data.backup", skill), tmp_path)
        assert ex.run("data.backup", {}) is None

    def test_non_dict_value_returns_none(self, tmp_path):
        def skill(params, ctx):
            return SkillResult(ok=True, value=42)

        ex = _executor(_registry_with("data.backup", skill), tmp_path)
        assert ex.run("data.backup", {}) is None
