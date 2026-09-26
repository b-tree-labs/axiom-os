# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The CLI printer must never crash on the result it was asked to print.

Found live on a node: `axi data backup-validate` ran its five checks
successfully, wrote the audit receipt, then died with KeyError
'connector' — the checks-pretty-printer was built for the preflight
shape ({connector, kind, checks: [{name, ok, message}]}) and fired on
ANY value containing "checks", including backup_validate's
({checks: [{name, status, detail}]}). A printer crash after the work
succeeded is the report-hides-the-effect class."""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.cli import _emit
from axiom.infra.skills import SkillResult


def _backup_validate_value():
    return {
        "target_root": "/x/backups",
        "newest": "b.dump",
        "checks": [
            {"name": "backup_exists", "status": "PASS", "detail": "1 artifact"},
            {"name": "restore_live", "status": "PASS", "detail": "132 tables"},
        ],
        "receipt": "authz-abc",
    }


def _preflight_value():
    return {
        "connector": "pushed-feed",
        "kind": "push",
        "ok": True,
        "checks": [
            {"name": "reachable", "ok": True, "message": "fine", "remediation": ""},
        ],
    }


def test_backup_validate_shape_prints_without_crashing(capsys):
    rc = _emit(SkillResult(ok=True, value=_backup_validate_value()), as_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "restore_live" in out
    assert "PASS" in out


def test_preflight_shape_keeps_its_pretty_rendering(capsys):
    rc = _emit(SkillResult(ok=True, value=_preflight_value()), as_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Connector: pushed-feed (push)" in out
    assert "✓ reachable" in out
