# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Each harness compose file builds a given image tag once.

CI's Install-Path job on main (run 41183764643) failed with
``failed to solve: image "docker.io/library/axiom-fed-node:dev": already exists``:
every node service in a topology carried the same ``build:`` and the same
``image:`` tag, and a current buildx bakes them in parallel and refuses the
second export of one tag. One service builds the image; the others name it
with ``pull_policy: never`` (it is local, never on a registry). Static, so it
runs without Docker.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parents[1] / "federation_lifecycle"
FILES = sorted(HERE.glob("docker-compose*.yml"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_a_tag_is_built_by_one_service(path: Path) -> None:
    services = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("services") or {}
    built = Counter(s["image"] for s in services.values() if "build" in s and "image" in s)
    assert all(n == 1 for n in built.values()), f"{path.name}: built more than once: {dict(built)}"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_a_service_reusing_a_local_tag_never_pulls_it(path: Path) -> None:
    services = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("services") or {}
    built = {s["image"] for s in services.values() if "build" in s and "image" in s}
    for name, s in services.items():
        if s.get("image") in built and "build" not in s:
            assert s.get("pull_policy") == "never", f"{path.name}: {name} would try to pull {s['image']}"
