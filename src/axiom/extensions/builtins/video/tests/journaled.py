# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A source's event journal, read straight off disk the way spec-video §Events lays it out.

Independent of ``ledger`` on purpose: a test that read the journal through the code
it is testing would agree with that code whatever the code wrote.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom.extensions.builtins.data_platform.daq import JournaledRecord


def journal_dir(state_dir: Path, stream: str) -> Path:
    return Path(state_dir) / "video" / "journal" / stream


def segments(state_dir: Path, stream: str) -> list[Path]:
    return sorted((journal_dir(state_dir, stream) / "segments").glob("seg-*.jsonl"))


def records(state_dir: Path, stream: str, *, feed: str | None = None) -> list[JournaledRecord]:
    """Every record in the order written; a line that does not parse fails the test."""
    out: list[JournaledRecord] = []
    for path in segments(state_dir, stream):
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = JournaledRecord.from_dict(json.loads(line))
            if feed is None or rec.envelope.feed == feed:
                out.append(rec)
    return out


def feeds(state_dir: Path, stream: str) -> set[str]:
    return {r.envelope.feed for r in records(state_dir, stream)}


def legacy_path(state_dir: Path) -> Path:
    """Where releases before the journal kept every event (spec-video §Events)."""
    return Path(state_dir) / "video" / "events.jsonl"
