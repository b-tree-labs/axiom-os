# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One module owns how CLI output looks, and departures are visible.

``axiom.infra.cli_format`` already held the answers — the standard indent,
ANSI-aware widths, terminal-aware layout, wrapping that keeps a wrapped
cell reading as one cell, rules, ``kv_line``, ``section_header``. Sixteen
modules used it. A seventeenth was written beside it by hand, and two verbs
ended up indenting the same table differently.

Nothing caught it because nothing was looking.

**This ratchets rather than demands.** The sweep that created it found 199
hand-rolled layouts across the tree, and a check that is red on the day it
lands gets switched off. So the existing ones are recorded as debt and may
stay at the count they have; what fails is a file growing a new one, or a
clean file acquiring its first.

The rule module is shared with ``axi ext lint``, deliberately: an extension
composed in from outside this tree is asked the same question, because a
rule that differs between the code we write and the code we accept is two
rules.
"""

from __future__ import annotations

import collections
import json
import pathlib

import pytest

from axiom.infra.cli_format_lint import DRAWN_RULE, PADDED_FIELD, scan_tree

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "axiom"
_BASELINE = pathlib.Path(__file__).parent / "cli_format_baseline.json"


def _counts() -> dict[str, int]:
    return dict(
        collections.Counter(
            str(o.path.relative_to(_SRC)) for o in scan_tree(_SRC)
        )
    )


def _baseline() -> dict[str, int]:
    return json.loads(_BASELINE.read_text(encoding="utf-8"))["files"]


def test_the_rules_can_match_and_do_not_over_match():
    """A scan matching nothing is green for the wrong reason; one matching
    everything gets ignored."""
    assert PADDED_FIELD.search('f"{name:<24} {n:>8}"')
    assert DRAWN_RULE.search('print("-" * 90)')
    assert not PADDED_FIELD.search('f"{value:.2f}"'), "precision is not a column"
    assert not PADDED_FIELD.search('f"{n:>2}"'), "two chars is not a column"


def test_no_file_grows_a_new_hand_rolled_layout():
    baseline, now = _baseline(), _counts()
    grew = {
        f: (baseline.get(f, 0), n) for f, n in now.items() if n > baseline.get(f, 0)
    }
    if not grew:
        return
    lines = "\n".join(
        f"    {f}: {was} → {is_}" for f, (was, is_) in sorted(grew.items())
    )
    pytest.fail(
        "hand-rolled CLI layout added. Use axiom.infra.cli_format (table / "
        "kv_line / section_header) so every verb indents the same way and "
        f"adapts to content:\n{lines}"
    )


def test_the_baseline_only_shrinks():
    """Converting a file must lower its number, so the debt is visible as it
    is paid rather than staying nominally the same forever."""
    baseline, now = _baseline(), _counts()
    stale = {f: c for f, c in baseline.items() if c > now.get(f, 0)}
    if stale:
        fixed = "\n".join(f"    {f}: {c} → {now.get(f, 0)}" for f, c in sorted(stale.items()))
        pytest.fail(
            "these files improved — lower them in cli_format_baseline.json "
            f"(delete the entry at zero) so the ratchet holds:\n{fixed}"
        )


def test_the_baseline_describes_real_files():
    missing = [f for f in _baseline() if not (_SRC / f).exists()]
    assert not missing, f"baseline names files that are gone: {missing}"
