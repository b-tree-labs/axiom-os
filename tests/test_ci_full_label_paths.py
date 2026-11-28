# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The CI-risk path set that applies ``ci:full`` on its own (#2441).

One data file, ``scripts/ci_full_paths.json``, holds the label name and the patterns. The
script ``scripts/ci_full_label.py`` reads it and nothing else decides.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "scripts" / "ci_full_paths.json"
SCRIPT = ROOT / "scripts" / "ci_full_label.py"

sys.path.insert(0, str(ROOT / "scripts"))
import ci_full_label as cfl  # noqa: E402

RISKY = [
    ".github/workflows/ci.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "scripts/testmon_blast_radius.py",
    "scripts/ci_full_label.py",
    "scripts/ci_whatever.py",
    "src/axiom/extensions/builtins/release/lander/checks.py",
    "src/axiom/extensions/builtins/release/order_shard.py",
    "tests/conftest.py",
    "tests/infra/conftest.py",
    "conftest.py",
    "pyproject.toml",
    "packages/axiom-tests/pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "requirements.txt",
    "tests/test_repo_hygiene.py",
    "tests/test_ci_fails_fast.py",
]
SAFE = [
    "README.md",
    "docs/guide.md",
    "src/axiom/chat/addressing.py",
    "tests/infra/test_cli_name_branding.py",
    "scripts/unit_shard_durations.json",
    "src/axiom/extensions/builtins/memory/mcp_server.py",
    "tests/release/test_build_provenance.py",
]


def test_the_data_file_names_the_label_and_the_patterns():
    data = json.loads(DATA.read_text())
    assert data["label"] == "ci:full"
    assert data["description"] == "run the main CI path on this pull request"
    assert data["patterns"] and all(isinstance(p, str) for p in data["patterns"])


def test_every_risk_path_the_owner_named_matches():
    for path in RISKY:
        assert cfl.matches(path), path


def test_ordinary_paths_do_not_match():
    for path in SAFE:
        assert not cfl.matches(path), path


def test_the_script_prints_the_matching_paths_and_exits_zero_either_way(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "paths"],
        input="README.md\n.github/workflows/ci.yml\nsrc/axiom/a.py\npyproject.toml\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out == [".github/workflows/ci.yml", "pyproject.toml"]
    none = subprocess.run(
        [sys.executable, str(SCRIPT), "paths"], input="README.md\n", capture_output=True, text=True
    )
    assert none.returncode == 0 and none.stdout == ""


def test_the_script_names_the_label():
    out = subprocess.run([sys.executable, str(SCRIPT), "label"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ci:full"


def test_the_lander_and_the_data_file_agree_on_the_label():
    from axiom.extensions.builtins.release.lander.checks import FULL_LABEL

    assert FULL_LABEL == json.loads(DATA.read_text())["label"]


def test_the_data_and_the_script_are_themselves_risk_paths():
    assert cfl.matches("scripts/ci_full_paths.json")
    assert cfl.matches("scripts/ci_full_label.py")


def test_the_contributing_template_names_the_label():
    template = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text()
    assert "ci:full" in template
