# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Public-mirror guard (ADR-078).

Makes the "public surface is clean" guarantee permanent: every file that would
ship to the public mirror (tracked minus ``mirror/exclude.txt``) is scanned for
institution/consumer identifiers, personal paths, and credential hints. A
regression — e.g. a new doc that names the domain consumer, or a fixture with a
``/Users/ben`` path — fails CI here instead of leaking into the public repo.

Also catches rot in ``mirror/exclude.txt`` (an entry that matches no file).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import build_public_mirror as mirror  # noqa: E402


def test_no_stale_exclude_patterns():
    stale = mirror.stale_exclude_patterns()
    assert stale == [], (
        "mirror/exclude.txt has entries matching no tracked file (stale):\n"
        + "\n".join(f"  - {s}" for s in stale)
    )


def test_public_surface_has_no_forbidden_terms():
    hits = mirror.scan_forbidden()
    assert hits == [], (
        f"{len(hits)} forbidden term(s) would leak into the public mirror. "
        "Genericize them, add the path to mirror/exclude.txt if it can't be "
        "genericized, or allowlist a legitimate occurrence in "
        "scripts/build_public_mirror.py:\n"
        + "\n".join(f"  {p}:{ln}: {line[:100]}" for p, ln, line in hits[:50])
    )
