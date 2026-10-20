# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A new CLI verb routes through the chokepoint, or says why not.

`invoke_capability` is where a verb gets three things it cannot get
anywhere else: the authority decision, the audit record, and the telemetry
row. A verb that dispatches around it is invisible to `axi agents status`,
to the capability ledger, and to the question "has an external harness
called this?".

That invisibility is not theoretical. On 2026-10-02 the ledger showed zero
invocations on a day the operator had used a verb, and 21 of 49 builtin
CLIs were routed — so an absence in the ledger meant nothing, and a reader
(a harness, reasoning about the platform) concluded a service had not run.

So this is a RATCHET, not a target. The 28 unrouted CLIs are grandfathered
in a baseline file. Entries only ever LEAVE it. A new extension that
dispatches directly fails here, because the gap grew by accretion and
nothing noticed — which is exactly what a baseline that can only shrink
prevents.
"""

from __future__ import annotations

import pathlib

BUILTINS = pathlib.Path(__file__).resolve().parents[2] / "src/axiom/extensions/builtins"
BASELINE = pathlib.Path(__file__).with_name("dispatch_chokepoint_baseline.txt")


def _grandfathered() -> set[str]:
    out = set()
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def _routed() -> tuple[set[str], set[str]]:
    routed, direct = set(), set()
    for f in sorted(BUILTINS.glob("*/cli.py")):
        text = f.read_text(encoding="utf-8", errors="replace")
        (routed if "invoke_capability" in text else direct).add(f.parent.name)
    return routed, direct


class TestTheRatchetOnlyTurnsOneWay:
    def test_a_new_cli_must_route_or_be_declared(self):
        """The anti-drift rule. Adding an unrouted CLI without saying so is
        how 28 of them accumulated."""
        _, direct = _routed()
        undeclared = sorted(direct - _grandfathered())
        assert not undeclared, (
            f"{undeclared} dispatch without `invoke_capability` and are not in "
            f"{BASELINE.name}. Route them — `invoke_capability(registry, "
            f"'<ext>.<verb>', params, ctx, surface=CLI_SURFACE)` — so their "
            "calls carry an authority decision, an audit record and a "
            "telemetry row. If one genuinely has no capabilities, add it to "
            "the baseline with a reason."
        )

    def test_the_baseline_names_no_one_who_has_since_been_routed(self):
        """A stale entry is a ratchet that stopped. Once a CLI is routed its
        line must go, or the file stops describing the gap."""
        routed, _ = _routed()
        stale = sorted(_grandfathered() & routed)
        assert not stale, (
            f"{stale} now route through invoke_capability — delete them from "
            f"{BASELINE.name}. The file records what is still missing, and an "
            "entry that is no longer true makes the number meaningless."
        )

    def test_the_baseline_names_no_one_who_does_not_exist(self):
        routed, direct = _routed()
        ghosts = sorted(_grandfathered() - (routed | direct))
        assert not ghosts, f"{ghosts} are in the baseline but have no cli.py"


class TestTheGapIsVisible:
    def test_the_count_is_reported_so_it_can_be_argued_about(self):
        """Not an assertion about the number — a statement of it. A ratchet
        nobody reads is one nobody turns."""
        routed, direct = _routed()
        total = len(routed) + len(direct)
        assert total, "no builtin CLIs found — the glob is wrong"
        # Printed on failure of anything else in this file, and available to
        # anyone running -s. The guard's job is that this only improves.
        print(f"\ndispatch chokepoint: {len(routed)}/{total} builtin CLIs routed")
