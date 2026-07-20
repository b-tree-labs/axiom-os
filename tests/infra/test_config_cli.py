# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-065 PR-1: subprocess smokes for ``axi config {validate|show|emit-schema}``.

CLI entry-point coverage per ``feedback_cli_subprocess_smoke_required``:
unit tests on the skill functions don't catch verb-routing or argparse
shape bugs. Each verb is exercised via ``python -m axiom.axiom_cli``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Make sure the in-repo src/ is importable even when no editable
    # install is active in the test environment.
    src = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [sys.executable, "-m", "axiom.axiom_cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        **kwargs,
    )


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "DemoConfig",
    "type": "object",
    "required": ["owner"],
    "additionalProperties": False,
    "properties": {
        "owner": {"type": "string", "pattern": "^@[a-z]+:[a-z]+$"},
        "threshold": {"type": "integer", "minimum": 0, "maximum": 100},
        "recipients": {"type": "array", "items": {"type": "string"}},
    },
}


@pytest.fixture
def schema_path(tmp_path) -> Path:
    p = tmp_path / "demo.schema.json"
    p.write_text(json.dumps(SCHEMA), encoding="utf-8")
    return p


def test_emit_schema_check_ok(schema_path):
    proc = _run(["config", "emit-schema", "--schema", str(schema_path), "--check"])
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "owner" in payload["properties"]


def test_emit_schema_check_rejects_missing_dollar_schema(tmp_path):
    bad = tmp_path / "bad.schema.json"
    bad.write_text(json.dumps({"type": "object", "properties": {"x": {"type": "string"}}}), encoding="utf-8")
    proc = _run(["config", "emit-schema", "--schema", str(bad), "--check"])
    assert proc.returncode != 0
    assert "missing $schema" in proc.stderr


def test_validate_happy_path(schema_path, tmp_path):
    cfg = tmp_path / "demo.json"
    cfg.write_text(
        json.dumps({"owner": "@alice:local", "threshold": 42, "recipients": ["a@b"]}),
        encoding="utf-8",
    )
    proc = _run(
        [
            "config",
            "validate",
            "demo_ext",
            "--schema",
            str(schema_path),
            "--config",
            str(cfg),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["extension"] == "demo_ext"


def test_validate_required_missing(schema_path, tmp_path):
    cfg = tmp_path / "demo.json"
    cfg.write_text(json.dumps({"threshold": 10}), encoding="utf-8")
    proc = _run(
        [
            "config",
            "validate",
            "demo_ext",
            "--schema",
            str(schema_path),
            "--config",
            str(cfg),
        ]
    )
    assert proc.returncode != 0
    assert "owner" in proc.stderr


def test_validate_out_of_range(schema_path, tmp_path):
    cfg = tmp_path / "demo.json"
    cfg.write_text(
        json.dumps({"owner": "@alice:local", "threshold": 999}),
        encoding="utf-8",
    )
    proc = _run(
        [
            "config",
            "validate",
            "demo_ext",
            "--schema",
            str(schema_path),
            "--config",
            str(cfg),
        ]
    )
    assert proc.returncode != 0
    assert "threshold" in proc.stderr


def test_validate_typo_caught_by_additional_properties(schema_path, tmp_path):
    cfg = tmp_path / "demo.json"
    cfg.write_text(
        json.dumps({"owner": "@alice:local", "thrshold": 5}),  # typo
        encoding="utf-8",
    )
    proc = _run(
        [
            "config",
            "validate",
            "demo_ext",
            "--schema",
            str(schema_path),
            "--config",
            str(cfg),
        ]
    )
    assert proc.returncode != 0
    assert "thrshold" in proc.stderr or "additional" in proc.stderr.lower()


def test_show_effective_after_bootstrap(schema_path, tmp_path):
    # Use --schema to ad-hoc register the extension's fields, then
    # show should emit the registry view.
    proc = _run(
        [
            "config",
            "show",
            "demo_ext",
            "--effective",
            "--schema",
            str(schema_path),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["extension"] == "demo_ext"
    assert "owner" in payload["effective"]
