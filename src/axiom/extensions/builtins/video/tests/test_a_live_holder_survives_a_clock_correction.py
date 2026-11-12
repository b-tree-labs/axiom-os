# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-007 holds when the wall clock is corrected while a source is captured.

Found on a WSL2 node: a capture ran for 26 minutes, the clock was corrected by
32 s, and from then on its own claim read as abandoned. ``status`` said nothing was
capturing it, ``stop`` said it was not running, and a second copy would have taken
the source from the one still holding it. The claim now names its holder by the
kernel's boot-relative start (``infra/pending_claim``), which a clock change does
not move.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from axiom.extensions.builtins.video import state as vs
from axiom.infra import pending_claim
from axiom.infra import process_registry as pr

pytestmark = pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="the boot-relative start comes from /proc"
)


@pytest.fixture
def correct_the_clock(monkeypatch):
    """``correct()``: from then on every start ``ps`` reports is 32 s later."""
    real = pr._parse_ps_line

    def shifted(line: str):
        info = real(line)
        return None if info is None else dataclasses.replace(info, start=info.start + 32.0)

    return lambda: monkeypatch.setattr(pr, "_parse_ps_line", shifted)


def test_the_holder_is_alive_and_keeps_its_source(state_dir, correct_the_clock):
    vs.claim_item("cam-1", state_dir).parent.mkdir(parents=True, exist_ok=True)
    held = pending_claim.claim(vs.claim_item("cam-1", state_dir), stale_after_s=vs.CLAIM_STALE_S)
    assert held is not None
    correct_the_clock()
    try:
        holder = vs.claim_holder("cam-1", state_dir)
        assert holder is not None and holder["alive"], holder
        assert not vs.release_claim("cam-1", state_dir), "a live holder's claim is not released"
        second = pending_claim.claim(vs.claim_item("cam-1", state_dir), stale_after_s=vs.CLAIM_STALE_S)
        assert second is None, "no second copy takes a source a live copy holds"
    finally:
        held.release()
