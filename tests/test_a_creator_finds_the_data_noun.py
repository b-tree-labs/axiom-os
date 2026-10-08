# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A creator finds the data commands from ``--help`` (#1159).

``data`` declared ``intent_groups = ["compose"]``, an intent no role activates,
so the help filter hid it from everyone, the builder role included, and it
also sat at ``advanced``, above the ``core`` tier that ``role add`` grants.
Two other nouns declared the same non-intent. The second test is the class
guard: an intent group that is not one of the declared intents is refused.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from axiom.cli.help_engine import INTENTS, UserCompetency, filter_commands

BUILTINS = Path(__file__).resolve().parents[1] / "src" / "axiom" / "extensions" / "builtins"


def _cmds() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for manifest in BUILTINS.glob("*/axiom-extension.toml"):
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        for p in data.get("extension", {}).get("provides", []):
            if p.get("kind") == "cmd" and p.get("noun"):
                out[p["noun"]] = {**p, "extension": manifest.parent.name}
    return out


def test_a_builder_sees_the_data_noun():
    shown = filter_commands(
        _cmds(), user_competency=UserCompetency(roles=("builder",), global_tier="core")
    )
    assert "data" in shown


def test_every_declared_intent_group_is_an_intent():
    bad = {
        noun: info["intent_groups"]
        for noun, info in _cmds().items()
        if set(info.get("intent_groups") or ()) - set(INTENTS)
    }
    assert not bad, f"intent groups no role can activate: {bad}"
