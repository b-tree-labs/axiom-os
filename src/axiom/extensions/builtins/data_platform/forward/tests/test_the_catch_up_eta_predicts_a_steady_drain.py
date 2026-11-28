# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The catch-up ETA predicts when a steady drain finishes.

The ETA is an extrapolation of the recent drain rate. Its accuracy can only be
judged against a drain whose speed does not change, so it is judged here, on a
fake clock where every backlog request takes the same time. The reconnect-flood
test runs against a real intake on a real machine, whose speed changes with
load; it checks the ETA against the rate the forwarder had seen, not against a
finish the machine's later speed decided.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.data_platform.forward.forwarder import Probe

from .test_a_slow_live_lane_never_starves_the_backlog_drain import _world

BACKLOG = 120
REQUEST_S = 1.0  # each backlog request takes one second: a steady 5 records a second


class SteadyIntake:
    def __init__(self, clock) -> None:
        self.clock = clock
        self.refusal = None

    def probe(self) -> Probe:
        return Probe(True, "")

    def send(self, rec: dict, body: bytes, *, lane: str = "live") -> tuple[bool, str]:
        self.clock.t += REQUEST_S
        return True, ""

    def beat(self, beat: dict) -> bool:
        return True


def test_a_third_of_the_way_in_the_eta_predicts_the_finish(tmp_path: Path):
    clock, _outbox, _intake, fwd = _world(tmp_path, backlog=BACKLOG)
    fwd.intake = SteadyIntake(clock)

    read: tuple[float, float] | None = None
    for _ in range(2000):
        fwd.forward_once()
        cu = fwd.health()["catch_up"]
        if cu.get("active") is False:
            break
        if read is None and cu.get("eta_s") is not None and cu.get("drained", 0) >= BACKLOG / 3:
            read = (clock.t, cu["eta_s"])
        clock.t += 0.1

    cu = fwd.health()["catch_up"]
    assert cu["active"] is False, cu
    assert read is not None, "no ETA was published during the drain"
    when, predicted = read
    actual = cu["finished_at"] - when
    assert abs(predicted - actual) <= 0.15 * actual + 1.0, (predicted, actual)
