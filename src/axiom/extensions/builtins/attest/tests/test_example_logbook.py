# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The example log (`docs/guides/examples/example_logbook/`) shows only what is in force.

The acceptance tests in `tests/logbook_acceptance/test_f2.py` load, discover and sign it. These
checks need no database: the example names no administration role and no deferred key, and the
commands its README tells a person to run exist.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import cli, logbooks
from ..logbooks import PLATFORM_ADMIN_ROLES, load_logbook

ROOT = Path(__file__).resolve().parents[6]
EXAMPLE = ROOT / "docs" / "guides" / "examples" / "example_logbook"
LOGBOOK_FILE = EXAMPLE / "logbook" / "example_log.toml"


def _deferred_keys() -> set[str]:
    tables = [v for k, v in vars(logbooks).items() if k.startswith("_") and isinstance(v, tuple)]
    return {key for t in tables if len(t) == 2 and isinstance(t[1], set) for key in t[1]}


def test_the_example_holds_no_administration_role():
    book = load_logbook(LOGBOOK_FILE)
    held = {r for t in book.types for r in t.roles}
    assert held == {"member", "lead"}
    assert not held & PLATFORM_ADMIN_ROLES


def test_the_example_declares_no_deferred_key():
    """A deferred key is accepted and reported; the example must not rely on one even in a
    comment-free form a later edit might uncomment."""
    deferred = _deferred_keys()
    # ``cosign`` left this set when unit L4 enforced it; its ``window`` and ``party`` keys remain.
    assert {"voice", "crosscheck", "seal", "export", "partition", "retention", "window", "party"} <= deferred
    assert load_logbook(LOGBOOK_FILE).not_yet_enforced == ()
    keys = set(re.findall(r"^\s*([a-z_]+)\s*=", LOGBOOK_FILE.read_text(encoding="utf-8"), re.M))
    keys |= set(re.findall(r"[{,]\s*([a-z_]+)\s*=", LOGBOOK_FILE.read_text(encoding="utf-8")))
    assert not keys & deferred, keys & deferred


def test_every_command_the_readme_names_exists():
    text = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    named = set(re.findall(r"axi attest ([a-z]+(?: [a-z]+)?)", text))
    assert named, "the README names the commands that show the example"
    parser = cli.get_parser()
    verbs = next(a for a in parser._actions if a.dest == "verb").choices
    for phrase in named:
        verb, _, sub = phrase.partition(" ")
        assert verb in verbs, phrase
        nested = [a for a in verbs[verb]._actions if a.dest.endswith("_verb")]
        if nested and sub:
            assert sub in nested[0].choices, phrase
