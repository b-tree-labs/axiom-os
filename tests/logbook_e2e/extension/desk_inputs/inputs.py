# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The desk log's site inputs, registered by the node at start (#1799): instruments, the session
seed, one site setting and a presenter. Nothing here imports attest internals beyond the four
public seams."""

from __future__ import annotations

SITE = "example-site"
CALLS: list[str] = []


def register(seams) -> None:
    from axiom.extensions.builtins.attest.field_sources import Instrument

    CALLS.append("register")

    def instruments(site: str):
        if site != SITE:
            return []
        return [Instrument("flow", "Flow", "L/min", {"kind": "magnitude", "value": "0.5"})]

    seams.field_source("instruments", instruments)
    seams.seed("e2e_log", "session", lambda site: 7000)
    seams.settings(lambda site: {"desk": {"checks": {"interval_minutes": 20}}} if site == SITE else {})
