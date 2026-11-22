# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""DC-21: the interface says "sign" only for a log entry or a document (#1935).

A held action is approved or rejected, so the approvals manifest text, the flag
description and the decision refusals use the act's own verb. Identifiers (the
`approvals_signed` flag key, skill names) are contracts and stay as they are.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "axiom"
ATTEST = SRC / "extensions" / "builtins" / "attest"


def _manifest():
    return tomllib.loads((ATTEST / "axiom-extension.toml").read_text())


def test_the_approvals_log_and_its_flag_do_not_say_sign():
    ext = _manifest()["extension"]
    approvals = next(p for p in ext["provides"] if p.get("id") == "approvals")
    assert "sign" not in approvals["description"].lower()
    flag = next(f for f in _manifest()["flags"] if f["key"] == "approvals_signed")
    assert "sign" not in flag["description"].lower()


def test_close_out_proposal_says_close_out_not_seal():
    ext = _manifest()["extension"]
    sk = next(p for p in ext["provides"] if p.get("name") == "attest.seal_propose")
    assert "seals" not in sk["description"]
    assert "close" in sk["description"].lower()


def test_decision_refusals_name_the_act_not_a_signature():
    for path in (ATTEST / "approvals.py", SRC / "infra" / "orchestrator" / "approval.py"):
        text = path.read_text()
        for m in re.finditer(r'(fail|DecisionRefused|reject)\((f?"[^"]*")', text):
            assert not re.search(r"\bsign(ed|s)?\b(?! in)", m.group(2)), (path.name, m.group(2))
        assert "Sign it on the card" not in text
