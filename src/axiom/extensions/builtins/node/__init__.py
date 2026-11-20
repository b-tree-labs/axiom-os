# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""KEEL: the one agent for the machine a node runs on (ADR-230).

The noun is ``node``; the verbs are plain English, one skill each:
``inventory`` (what runs here), ``vitals`` (how it is doing), ``doctor``
(what is wrong and the fix), ``drift`` (where it disagrees with itself or
its tracked source), ``report`` (all of that as one document), ``declare``
(tell KEEL about what Axiom did not install) and ``clean`` (reclaim, as a
plan first). KEEL is a Steward and a Sensor in AEOS 1.0 §3 terms; the ship
is its figure (docs/conventions/the-ship.md): inventory, soundings, drift,
upkeep.

Everything observable is computed from the machine or from the node's
declarations each time it is asked; KEEL carries no state of its own
between invocations (AEOS 1.0 §2.13).
"""

from __future__ import annotations

from pathlib import Path


def keel_persona_path() -> Path:
    """Where KEEL's persona lives; the manifest's agent entry points here."""
    return Path(__file__).parent / "agents" / "keel" / "persona.md"


__all__ = ["keel_persona_path"]
