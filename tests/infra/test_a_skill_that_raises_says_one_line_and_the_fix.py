# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An uncaught exception in a skill prints one clear line plus the fix.

The traceback goes to a log file by default and appears in the result only
when AXIOM_DEBUG is set, so developers keep the full detail.
"""

from __future__ import annotations

import logging
from pathlib import Path

from axiom.infra.skills import SkillContext, SkillRegistry


def _boom_registry():
    reg = SkillRegistry()

    def boom(params, ctx):
        raise RuntimeError("oops")

    reg.register("test.boom", boom)
    return reg


def _ctx(reg, tmp_path: Path):
    return SkillContext(
        registry=reg, state_dir=tmp_path, logger=logging.getLogger("skill-test"), user_prompt=None
    )


def test_default_result_has_one_line_a_hint_and_no_traceback(tmp_path, monkeypatch):
    monkeypatch.delenv("AXIOM_DEBUG", raising=False)
    reg = _boom_registry()
    result = reg.invoke("test.boom", {}, _ctx(reg, tmp_path))
    assert result.ok is False
    text = "\n".join(result.errors)
    assert "RuntimeError: oops" in text
    assert "Traceback" not in text and 'File "' not in text
    assert "AXIOM_DEBUG=1" in text  # the fix: how to see more
    assert "skill-errors.log" in text  # where the detail went


def test_the_traceback_is_kept_in_a_log_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AXIOM_DEBUG", raising=False)
    reg = _boom_registry()
    reg.invoke("test.boom", {}, _ctx(reg, tmp_path))
    log = tmp_path / "logs" / "skill-errors.log"
    body = log.read_text()
    assert "test.boom" in body and "Traceback" in body and "RuntimeError: oops" in body


def test_debug_flag_keeps_the_traceback_in_the_result(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_DEBUG", "1")
    reg = _boom_registry()
    result = reg.invoke("test.boom", {}, _ctx(reg, tmp_path))
    assert any("Traceback" in e for e in result.errors)


def test_an_unwritable_log_never_breaks_the_result(tmp_path, monkeypatch):
    monkeypatch.delenv("AXIOM_DEBUG", raising=False)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    reg = _boom_registry()
    result = reg.invoke("test.boom", {}, _ctx(reg, blocker))  # state_dir is a file
    assert result.ok is False and any("oops" in e for e in result.errors)
