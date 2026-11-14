# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the state-chaos suite."""

from __future__ import annotations

import pytest

from .artifacts import ARTIFACTS, populate
from .harness import ChaosRoot, restore_permissions


@pytest.fixture
def chaos_state(tmp_path):
    """A disposable state tree, fully populated by the REAL writers.

    Every persisted artifact in the catalog exists under ``chaos.state`` and
    the environment returned by ``chaos.env()`` points every path at it.
    Nothing here can reach ``~/.axi``: the root is asserted to be temp.
    """
    chaos = ChaosRoot(tmp_path / "chaos")
    chaos.populated = {}
    yield chaos
    restore_permissions(chaos.root)


@pytest.fixture
def populate_all(chaos_state):
    """Populate the catalog into ``chaos_state`` (each writer once)."""
    seen = set()
    for art in ARTIFACTS:
        if art.populate in seen:
            continue
        seen.add(art.populate)
        chaos_state.populated[art.id] = populate(art, chaos_state)
    return chaos_state
