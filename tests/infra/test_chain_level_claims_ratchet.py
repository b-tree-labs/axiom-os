# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The repo's own ratchet, expressed as claimed level per extension (AEOS 0.2 section 4.11).

``tests/infra/skill_chaining_debt.txt`` names the skills still owing declarations
and only shrinks. This is the same fact one level up, and the two cannot
disagree:

* an extension with registered skills and none on the debt list claims Level B,
  so it cannot quietly regress to owing declarations;
* an extension claiming B or C has no skill on the debt list;
* every claim a built-in manifest makes is verified by ``axi ext lint``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.cli.ext.commands.lint import chain_assessment
from axiom.infra import skill_chain_lint as scl


@pytest.fixture(scope="module")
def result():
    return scl.lint()


def test_an_extension_with_no_debt_claims_level_b(result):
    claims = scl.claimed_levels()
    registered = {a.extension for a in result.skills.values()}
    owing = {a.extension for a in result.skills.values() if not a.clean}
    unclaimed = sorted(e for e in registered - owing if claims.get(e, "A") == "A")
    assert not unclaimed, (
        "these built-in extensions have no skill owing declarations but do not claim it: add "
        'aeos_version = "0.2.0" and chain_level = "B" to their manifest [extension] table so the '
        "gain cannot be lost: " + ", ".join(unclaimed)
    )


def test_an_extension_claiming_b_or_c_has_no_skill_on_the_debt_list(result):
    claims = scl.claimed_levels()
    contradictions = sorted(
        {
            a.extension
            for a in result.skills.values()
            if not a.clean and claims.get(a.extension, "A") != "A"
        }
    )
    assert not contradictions, (
        f"claims above Level A while skills still owe declarations: {contradictions}"
    )


def test_every_builtin_claim_is_verified_by_lint():
    root = scl.builtins_root()
    failures = {}
    for ext, level in scl.claimed_levels().items():
        if level == "A":
            continue
        a = chain_assessment(Path(root) / ext)
        if a.claimed != level or "ABC".index(a.achieved) < "ABC".index(level):
            failures[ext] = (
                level,
                a.achieved,
                [f.code for f in a.findings if f.severity == "error"],
            )
    assert not failures, failures
