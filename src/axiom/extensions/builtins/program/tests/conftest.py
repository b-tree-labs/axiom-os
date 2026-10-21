# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the program extension's tests.

Every name in the fixture data is invented and generic — example people,
example lanes, an example tracker host. The data's *shape* mirrors the
``axiom.program/0.1`` schema exactly; its vocabulary deliberately does not
mirror anybody's deployment.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from axiom.infra.skills import SkillContext, SkillRegistry

#: A complete, valid ``axiom.program/0.1`` instance.
#:
#: Deliberate coverage built into the fixture:
#: - ``i-one``  — span item with every optional field (pct, status, issue).
#: - ``i-two``  — span item, committed, no tracker issue.
#: - ``i-three``— milestone (single ``date``), proposed, with an issue.
#: - ``i-four`` — fixed date with NO owner, NO status, NO pct: the
#:   absent-is-absent case every reporting test leans on.
#: - ``example_bindings`` — a host-qualified binding block the schema does
#:   not enumerate; generic handling must carry it through untouched.
GENERIC_PROGRAM: dict[str, Any] = {
    "schema": "axiom.program/0.1",
    "program": {
        "id": "example-program",
        "name": "Example Program",
        "as_of": "2026-10-05",
        "deputy": "@casey:example-org",
        "tracker": {
            "kind": "generic",
            "host": "tracker.example.org",
            "project_id": 7,
        },
        "design_doc": "https://docs.example.org/design",
        "children": [],
    },
    "lanes": [
        {"id": "alpha", "name": "Alpha", "color": "lane-alpha"},
        {"id": "beta", "name": "Beta", "color": "lane-beta"},
    ],
    "people": [
        {
            "principal": "@casey:example-org",
            "name": "Casey Example",
            "lanes": ["alpha", "beta"],
            "drives": "Coordination; the alpha increment",
        },
        {
            "principal": "@dana:example-org",
            "name": "Dana Example",
            "lanes": ["alpha"],
            "drives": "Alpha delivery",
        },
        {
            "principal": "@rowan:example-org",
            "name": "Rowan Example",
            "lanes": [],
            "drives": "Support",
        },
    ],
    "schedule": [
        {
            "id": "i-one",
            "label": "First increment",
            "owner": "@casey:example-org",
            "start": "2026-10-05",
            "end": "2026-10-16",
            "lane": "alpha",
            "pct": 15,
            "status": "proposed",
            "issue": 42,
        },
        {
            "id": "i-two",
            "label": "Second increment",
            "owner": "@dana:example-org",
            "start": "2026-10-05",
            "end": "2026-10-09",
            "lane": "alpha",
            "pct": 30,
            "status": "committed",
        },
        {
            "id": "i-three",
            "label": "Demo day",
            "owner": "@casey:example-org",
            "date": "2026-10-14",
            "lane": "beta",
            "kind": "milestone",
            "status": "proposed",
            "issue": 57,
        },
        {
            "id": "i-four",
            "label": "Fixed window",
            "date": "2026-12-15",
            "lane": "beta",
            "kind": "fixed",
        },
    ],
    "provenance": {
        "maintained_by": "manual",
        "last_sync": "2026-10-05T22:30:00Z",
    },
    "example_bindings": {
        "root": 100,
        "lanes": {"alpha": 101, "beta": 102},
    },
}


@pytest.fixture
def data_dict() -> dict[str, Any]:
    """A deep copy tests may mutate freely."""
    return copy.deepcopy(GENERIC_PROGRAM)


@pytest.fixture
def data_file(tmp_path: Path, data_dict: dict[str, Any]) -> Path:
    """The fixture program written to disk as a data file."""
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
    return path


@pytest.fixture
def write_data(tmp_path: Path):
    """Write an arbitrary (possibly broken) dict as a data file."""

    def _write(payload: Any, name: str = "data.json") -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def ctx(tmp_path: Path) -> SkillContext:
    """A clean CLI-surface skill context whose state never leaves the test tree.

    ``surface="cli"`` because the CLI is the one surface allowed to name a
    data file with ``data``; the serving surfaces read the node's own file
    (see ``state_ctx`` and the projection tests).
    """
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path / "state",
        logger=logging.getLogger("test.program"),
        user_prompt=None,
        surface="cli",
    )


@pytest.fixture
def state_dir(tmp_path: Path, data_dict: dict[str, Any]) -> Path:
    """A node state dir holding the fixture program at the default path."""
    root = tmp_path / "node-state"
    (root / "program").mkdir(parents=True)
    (root / "program" / "data.json").write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
    return root
