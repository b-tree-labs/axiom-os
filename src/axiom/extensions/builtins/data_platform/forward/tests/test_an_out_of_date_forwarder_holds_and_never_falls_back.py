# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A forwarder says what it runs, and one an intake finds too old holds everything and does not go round it.

Real nodes, as in the reconnect tests: a site's local node, an upstream intake,
issued keys. The intake declares the oldest sender it accepts.
"""

from __future__ import annotations

import time

from axiom.extensions.builtins.data_platform import compat

from .test_a_reconnect_flood_sends_live_first_and_paces_the_backlog import (
    _assert_exactly_once,
    _build_backlog,
    _forwarder,
    site,  # noqa: F401
)


def _run(fwd, seconds: float) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        fwd.forward_once()
        time.sleep(0.05)


def test_a_current_forwarder_is_accepted_under_a_declared_minimum(site):  # noqa: F811
    local, upstream, local_key, up_key, source = site
    _build_backlog(local, local_key, source, 10)
    upstream.up(**{compat.MIN_CLIENT_ENV: "axiom-os-lm>=0.1"})
    fwd = _forwarder(local, upstream, up_key, source, live_window_s=0.5)
    t0 = time.time()
    while time.time() - t0 < 30 and len(upstream.outbox_records()) < 10:
        fwd.forward_once()
        time.sleep(0.05)
    _assert_exactly_once(local, upstream)


def test_a_too_old_forwarder_holds_says_why_and_does_not_fall_back(site):  # noqa: F811
    local, upstream, local_key, up_key, source = site
    _build_backlog(local, local_key, source, 20)
    upstream.up(**{compat.MIN_CLIENT_ENV: "axiom-os-lm>=999.0"})
    fwd = _forwarder(local, upstream, up_key, source, live_window_s=0.5)
    _run(fwd, 4)
    h = fwd.health()
    assert upstream.outbox_records() == []  # nothing sent past the window
    assert h["after"] == 0  # and nothing skipped
    assert h["refusal"]["code"] == 426, h
    assert "axiom-os-lm>=999.0" in h["refusal"]["update_required"]["requirement"]
    assert h["refusal"]["update_required"]["command"]
    assert h["catch_up"].get("refused_426", 0) <= 2, h["catch_up"]  # asked once, then waits
    # Never routed around the intake: its only switch is the start-up one to it.
    assert h["current"] == "intake" and all(sw["to"] == "intake" for sw in h["switches"]), h

    # Updated (here: the intake relaxes its minimum and is restarted): everything held goes, once.
    upstream.down()
    upstream.up()
    fwd.intake.refusal = None
    fwd.catch_up.pop("wait_until", None)
    t0 = time.time()
    while time.time() - t0 < 30 and len(upstream.outbox_records()) < 20:
        fwd.forward_once()
        time.sleep(0.05)
    _assert_exactly_once(local, upstream)
