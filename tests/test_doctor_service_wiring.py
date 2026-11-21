# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""`doctor` fails when the service install points at a tree that is gone (#1693)."""

from __future__ import annotations

import json

from axiom.cli import doctor
from axiom.cli.doctor import CheckStatus


def test_a_deleted_editable_tree_fails_with_path_and_fix(tmp_path):
    site = tmp_path / "lib" / "site-packages"
    site.mkdir(parents=True)
    gone = tmp_path / "worktrees" / "axiom-service"
    (site / "__editable__.axiom.pth").write_text(str(gone) + "/src\n")
    res = doctor.check_service_wiring(site_dirs=[site], claude_config=tmp_path / "none.json")
    assert res.status is CheckStatus.ERROR
    assert str(gone) in res.summary
    assert "pip install" in (res.fix_hint or "")


def test_a_present_tree_is_ok(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    tree = tmp_path / "src"
    tree.mkdir()
    (site / "x.pth").write_text(f"# comment\nimport sys\n{tree}\n")
    res = doctor.check_service_wiring(site_dirs=[site], claude_config=tmp_path / "none.json")
    assert res.status is CheckStatus.OK


def test_an_mcp_command_that_does_not_resolve_fails(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"axiom": {"command": str(tmp_path / "nope/python")}}}))
    res = doctor.check_service_wiring(site_dirs=[], claude_config=cfg)
    assert res.status is CheckStatus.ERROR
    assert "nope/python" in res.summary


def test_the_check_is_registered():
    assert "Service install wiring" in [c.name for c in doctor.default_checks()]
