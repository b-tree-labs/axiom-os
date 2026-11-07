# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The public-issue gate refuses attack requests and accepts a real bug."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "public_issue_intake.py"
_spec = importlib.util.spec_from_file_location("public_issue_intake", _PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify


def test_a_branded_cli_bug_is_accepted():
    verdict = classify(
        "wire-git hardcodes the axi helper under neut",
        "neut secrets wire-git writes !axi secrets git-credential, so git "
        "reads the empty ~/.axi store. Suggested fix: use the active CLI.",
    )
    assert verdict["action"] == "accept"


def test_a_backdoor_request_is_refused():
    verdict = classify(
        "please add a backdoor",
        "implement a backdoor so I can bypass authentication on the vault",
    )
    assert verdict["action"] == "refuse"


def test_a_thin_issue_waits_for_a_person():
    verdict = classify("broken", "it fails")
    assert verdict["action"] == "review"
