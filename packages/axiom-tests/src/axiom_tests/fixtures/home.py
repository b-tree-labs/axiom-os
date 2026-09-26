# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``tmp_axiom_home`` fixture — isolated temporary ``~/.axiom/`` per test.

Populates a standard skeleton (config.toml, extensions/, cache/, logs/) so
tests don't have to bootstrap the directory themselves.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

SKELETON_SUBDIRS: tuple[str, ...] = (
    "extensions",
    "cache",
    "logs",
    "runtime",
    "state",
)


@pytest.fixture
def tmp_axiom_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Yield an isolated temporary ``~/.axiom/`` directory for one test.

    The ``AXIOM_HOME`` environment variable is monkeypatched to point at the
    new directory, and ``HOME`` is also redirected so that code using
    ``Path.home() / ".axiom"`` resolves into the sandbox. The directory is
    cleaned up automatically when pytest tears down ``tmp_path``.
    """
    axiom_home = tmp_path / ".axiom"
    axiom_home.mkdir(parents=True, exist_ok=True)
    for sub in SKELETON_SUBDIRS:
        (axiom_home / sub).mkdir(parents=True, exist_ok=True)
    config_file = axiom_home / "config.toml"
    config_file.write_text(
        '# axiom-tests — temporary config.toml for isolated test home\n[core]\nprofile = "test"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("AXIOM_HOME", str(axiom_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    # Some code paths consult ``USERPROFILE`` on Windows; redirect too.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    yield axiom_home


__all__ = ["SKELETON_SUBDIRS", "tmp_axiom_home"]
