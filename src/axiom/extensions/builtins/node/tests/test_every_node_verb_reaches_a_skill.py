# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every ``axi node`` verb names a skill the extension actually registers."""

from __future__ import annotations

import re
from pathlib import Path

from axiom.extensions.builtins.node.commands import cli

MANIFEST = Path(cli.__file__).parents[1] / "axiom-extension.toml"


def _registered() -> set[str]:
    return set(re.findall(r'^name = "(node\.[a-z_]+)"$', MANIFEST.read_text(encoding="utf-8"), re.M))


def test_every_verb_resolves_to_a_registered_skill():
    missing = [v for v in cli.VERBS if cli.skill_for(v) not in _registered()]
    assert not missing, missing


def test_the_guard_can_fail():
    assert "node.reap" not in _registered()
