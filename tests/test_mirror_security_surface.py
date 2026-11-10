# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The mirror-security surface that ships, and the workflows that must not."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PIN = re.compile(r"@[0-9a-f]{40}\s+#\s+v")


def _text(path: Path) -> str:
    return path.read_text()


def _workflow(name: str) -> str:
    """Workflows are excluded from the public mirror. Skip there; run in source."""
    path = WORKFLOWS / name
    if not path.is_file():
        pytest.skip("workflow is not in this tree")
    return path.read_text()


def test_process_doc_is_public_and_generic():
    doc = ROOT / "docs" / "MIRROR_SECURITY.md"
    text = _text(doc)
    flat = " ".join(text.split())
    for phrase in (
        "untrusted",
        "failing test",
        "Do not paste",
        "How old is the account",
        "no secrets",
        "private advisory",
        "after the release",
        "Fixed in <version>, thanks for reporting",
        "from-public",
        "dry run",
        "Semgrep",
        "pip-audit",
        "error severity",
        "high or critical",
    ):
        assert phrase in flat, phrase
    # Assembled so the public-surface scanner does not see the literals here.
    banned_terms = (
        "axiom-os-private",
        "MIRROR_PUSH_TOKEN",
        "benjaminbooth",
        "bbooth@",
        "ute" + "xas",
        "/Users/" + "ben",
        "neut" + "ron-os",
    )
    folded = text.lower()
    for banned in banned_terms:
        assert banned.lower() not in folded
    assert (doc.parent / "../SECURITY.md").resolve().is_file()
    exclude = _text(ROOT / "mirror" / "exclude.txt")
    assert ".github/workflows/" in exclude
    assert "MIRROR_SECURITY.md" not in exclude


def test_codeowners_covers_sensitive_paths_that_exist():
    text = _text(ROOT / ".github" / "CODEOWNERS")
    assert "@benjaminbooth" in text
    rules = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pattern, owner = stripped.split()
        assert owner == "@benjaminbooth"
        rules.append(pattern)
    assert "/.github/" in rules
    for pattern in rules:
        if pattern.startswith("/"):
            assert (ROOT / pattern.lstrip("/")).exists(), pattern
        elif "*" not in pattern:
            assert any(ROOT.rglob(pattern)), pattern


def test_pr_template_requires_owner_signoff():
    text = _text(ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md")
    assert "@benjaminbooth" in text
    lowered = text.lower()
    for phrase in (
        "auth",
        "validation",
        "sandbox",
        "redaction",
        "telemetry",
        "eval",
        "shell",
        "dependency",
        "from-public",
        "failing test",
    ):
        assert phrase in lowered


def test_close_workflow_is_idempotent_and_uses_the_mirror_token():
    text = _workflow("public-issue-close.yml")
    assert "Public Mirror" in text
    assert "workflow_run" in text
    assert "release:" in text
    assert "workflow_dispatch" in text
    assert "dry_run:" in text
    assert "default: true" in text
    assert "secrets.MIRROR_PUSH_TOKEN" in text
    assert "mirror_public_issues.py close" in text
    assert "pull_request_target" not in text
    assert "ubuntu-latest" not in text
    assert "thanks for reporting" not in text


def test_audit_workflow_is_weekly_and_private():
    text = _workflow("public-reply-audit.yml")
    assert "0 8 * * 1" in text
    assert "mirror_public_issues.py audit" in text
    assert "default: true" in text
    assert "secrets.MIRROR_PUSH_TOKEN" in text
    assert "issues: write" in text
    assert "pull_request_target" not in text


def test_semgrep_and_pip_audit_replace_paid_scanners():
    assert not (WORKFLOWS / "codeql.yml").exists()
    assert not (WORKFLOWS / "dependency-review.yml").exists()
    semgrep = _workflow("semgrep.yml")
    audit = _workflow("pip-audit.yml")
    for text in (semgrep, audit):
        assert "pull_request:" in text
        assert "branches: [main]" in text
        assert "pull_request_target" not in text
        assert "ubuntu-latest" not in text
        assert "codeql" not in text.lower()
        assert "upload-sarif" not in text.lower()
        assert "--sarif" not in text.lower()
        assert "upload-artifact@" in text
    assert "semgrep/semgrep:1.182.0" in semgrep
    assert "semgrep scan" in semgrep
    for pack in ("p/python", "p/javascript", "p/typescript", "p/terraform", "p/secrets", "p/github-actions"):
        assert pack in semgrep
    assert "--severity=ERROR" in semgrep
    assert "--error" in semgrep
    assert "pip-audit" in audit
    assert "pip_audit_gate.py" in audit
    assert "osv" in audit


def test_new_workflows_pin_actions_and_parse():
    mirror = _workflow("mirror.yml")
    assert "Public mirror push recorded" in mirror
    for name in (
        "public-issue-close.yml",
        "public-reply-audit.yml",
        "semgrep.yml",
        "pip-audit.yml",
        "mirror.yml",
    ):
        text = _workflow(name)
        yaml.safe_load(text)
        assert "ubuntu-24.04" in text
        for line in text.splitlines():
            if "uses:" not in line:
                continue
            assert PIN.search(line), line
