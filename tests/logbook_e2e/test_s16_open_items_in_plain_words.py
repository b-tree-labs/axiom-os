# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S16 A close-out's open items in plain words (Log views design 2026-11-13, build change 27).

The appkit design (``docs/design/proposed-2026-11-13/log-views/``, Close out) lists the open items
grouped by kind, each a plain line a person reads ("#13 Replaced the filter on line B", "A note
proposed for you at 13:40"). The node used to send only ``summary`` strings carrying the type's code
and a person's handle ("a NOTE draft for @ann:example-site is still open"). Each open item now
carries the parts a page names it by, and its ``summary`` (which a close-out records) reads in words.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from .conftest import ANN, LOG, SITE, check, draft, open_session, sign_cli, sign_web

HANDLE = re.compile(r"@[\w.\-]+:[\w.\-]+")
CODE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z]+)*\b")


def _preview(world):
    from axiom.extensions.builtins.attest import obligations, service

    t0 = world.clock["t"]
    open_session(world)
    check(world)
    sign_cli(ANN, "NOTE", {"text": "left unacknowledged"}, title="Pump noisy at start-up")
    sign_web(ANN, draft(ANN, "CHANGE", {"text": "a setting moved"}, title="Replaced the filter on line B"),
             device="desk-1")  # fmt: skip
    draft(ANN, "NOTE", {"text": "left open"})
    obligations.tick(SITE, LOG, now=t0 + timedelta(minutes=45), state_dir=world.state_dir)
    world.clock["t"] = t0 + timedelta(minutes=46)
    return service.seal_preview(SITE, LOG, boundary="session")["open_items"]


@pytest.mark.acceptance("LV-27-A1")
def test_lv_27_a1_every_open_item_summary_reads_in_words_with_no_code_or_handle(world):
    items = _preview(world)
    assert {"pending_cosign", "unacknowledged", "open_draft", "missed_obligation", "open_interval"} <= {
        i["kind"] for i in items
    }
    for i in items:
        assert not HANDLE.search(i["summary"]), i["summary"]
        assert not CODE.search(i["summary"]), i["summary"]
        assert i["summary"][0].isupper() or i["summary"][0] == "#", i["summary"]


@pytest.mark.acceptance("LV-27-A2")
def test_lv_27_a2_each_open_item_carries_the_parts_a_page_names_it_by(world):
    by = {}
    for i in _preview(world):
        by.setdefault(i["kind"], []).append(i)
    cosign = by["pending_cosign"][0]
    assert cosign["title"] == "Replaced the filter on line B" and isinstance(cosign["seq"], int)
    assert cosign["summary"] == f"#{cosign['seq']} Replaced the filter on line B waits for a co-signature"
    acked = [i for i in by["unacknowledged"] if i["title"] == "Pump noisy at start-up"][0]
    assert acked["summary"] == f"#{acked['seq']} Pump noisy at start-up is not yet acknowledged"
    d = by["open_draft"][0]
    assert d["type"] == "Note" and d["for"] == ANN.handle and d["at"]
    assert d["summary"] == "A note draft is still open"
    missed = by["missed_obligation"][0]
    assert missed["summary"].startswith("Check cadence was missed in session ")
    interval = by["open_interval"][0]
    assert re.fullmatch(r"Session \d+ is still open", interval["summary"])
