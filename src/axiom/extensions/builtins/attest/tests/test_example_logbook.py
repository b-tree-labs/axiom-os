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

from .. import log_cli, logbooks
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
    # ``cosign`` (L4), ``seal`` (L5), ``export`` (L6), cosign's ``window`` (X1) and ``party``
    # (#1807) left this set when enforced.
    assert {"voice", "crosscheck", "partition", "retention"} <= deferred
    assert not {"export", "window", "party"} & deferred
    assert load_logbook(LOGBOOK_FILE).not_yet_enforced == ()
    keys = set(re.findall(r"^\s*([a-z_]+)\s*=", LOGBOOK_FILE.read_text(encoding="utf-8"), re.M))
    keys |= set(re.findall(r"[{,]\s*([a-z_]+)\s*=", LOGBOOK_FILE.read_text(encoding="utf-8")))
    assert not keys & deferred, keys & deferred


def test_every_command_the_readme_names_exists():
    """Every `axi log ...` the README names parses: a noun-level verb, or one of a log's."""
    text = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    named = set(re.findall(r"axi log ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", text))
    assert named, "the README names the commands that show the example"
    assert "axi attest" not in text, "the README names the old form"

    def choices(parser):
        return next(a for a in parser._actions if a.dest == "verb").choices

    noun, per_log = choices(log_cli.noun_parser()), choices(log_cli.log_parser("example-log"))
    for phrase in named:
        first, _, second = phrase.partition(" ")
        if first in noun:
            nested = [a for a in noun[first]._actions if a.dest.endswith("_verb")]
            if nested and second:
                assert second in nested[0].choices, phrase
        else:
            assert first == "example-log", phrase
            assert second in per_log, phrase
