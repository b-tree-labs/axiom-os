# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the git-init offer flow (axiom.infra.git_setup)."""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.infra import git_setup


def test_returns_true_when_already_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(git_setup, "git_available", lambda: True)
    monkeypatch.setattr(git_setup, "is_inside_work_tree", lambda _p: True)
    out: list[str] = []
    assert git_setup.ensure_repo_or_offer_init(tmp_path, output_fn=out.append) is True
    assert out == []  # silent on the happy path


def test_false_when_git_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(git_setup, "git_available", lambda: False)
    out: list[str] = []
    assert git_setup.ensure_repo_or_offer_init(tmp_path, output_fn=out.append) is False
    assert any("not installed" in line for line in out)


def test_non_interactive_prints_instructions_not_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(git_setup, "git_available", lambda: True)
    monkeypatch.setattr(git_setup, "is_inside_work_tree", lambda _p: False)
    monkeypatch.setattr(
        git_setup, "init_repo", lambda *_a, **_k: pytest.fail("must not init")
    )
    out: list[str] = []
    ok = git_setup.ensure_repo_or_offer_init(
        tmp_path, interactive=False, output_fn=out.append
    )
    assert ok is False
    assert any("git init" in line for line in out)


def test_assume_yes_inits_without_prompting(monkeypatch, tmp_path):
    monkeypatch.setattr(git_setup, "git_available", lambda: True)
    monkeypatch.setattr(git_setup, "is_inside_work_tree", lambda _p: False)
    called: dict[str, Path] = {}
    monkeypatch.setattr(
        git_setup, "init_repo", lambda p, **_k: called.setdefault("p", p)
    )
    out: list[str] = []
    ok = git_setup.ensure_repo_or_offer_init(
        tmp_path, assume_yes=True, output_fn=out.append
    )
    assert ok is True
    assert called["p"] == Path(tmp_path)


def test_interactive_yes_inits(monkeypatch, tmp_path):
    monkeypatch.setattr(git_setup, "git_available", lambda: True)
    monkeypatch.setattr(git_setup, "is_inside_work_tree", lambda _p: False)
    inited: dict[str, Path] = {}
    monkeypatch.setattr(
        git_setup, "init_repo", lambda p, **_k: inited.setdefault("p", p)
    )
    ok = git_setup.ensure_repo_or_offer_init(
        tmp_path, interactive=True, input_fn=lambda _p: "y", output_fn=lambda _s: None
    )
    assert ok is True
    assert inited["p"] == Path(tmp_path)


def test_interactive_no_declines(monkeypatch, tmp_path):
    monkeypatch.setattr(git_setup, "git_available", lambda: True)
    monkeypatch.setattr(git_setup, "is_inside_work_tree", lambda _p: False)
    monkeypatch.setattr(
        git_setup, "init_repo", lambda *_a, **_k: pytest.fail("must not init")
    )
    out: list[str] = []
    ok = git_setup.ensure_repo_or_offer_init(
        tmp_path, interactive=True, input_fn=lambda _p: "n", output_fn=out.append
    )
    assert ok is False
    assert any("Aborted" in line for line in out)
