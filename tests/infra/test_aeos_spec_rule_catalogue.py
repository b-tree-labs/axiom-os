# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The spec's rule table and the code's rule catalogue are one list (AEOS 0.2 section 4.11.8)."""

from __future__ import annotations

import re
from pathlib import Path

from axiom.infra import chain_levels as cl

SPEC = Path(__file__).resolve().parents[2] / "docs" / "specs" / "spec-aeos-0.1.md"


def _table() -> dict[str, tuple[str, str]]:
    text = SPEC.read_text(encoding="utf-8")
    section = text[text.index("#### 4.11.8 Rule catalogue") : text.index("#### 4.11.9")]
    rows = re.findall(r"^\| (AEOS\d+) \| ([^|]+) \| ([^|]+) \|", section, flags=re.M)
    return {r[0]: (r[1].strip(), r[2].strip()) for r in rows}


def test_the_spec_lists_exactly_the_rules_the_code_defines():
    assert set(_table()) == set(cl.RULE_IDS)


def test_severity_and_level_agree_between_spec_and_code():
    table = _table()
    for rule in cl.RULES:
        level, severity = table[rule.id]
        assert severity == rule.severity, rule.id
        expected = {"-": "note"}.get(rule.level, rule.level)
        assert level == expected, rule.id
