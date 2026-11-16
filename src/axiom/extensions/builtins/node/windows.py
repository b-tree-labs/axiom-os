# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Maintenance windows: who is working on this node, on what, until when.

On 2026-10-31 three agents in two sessions coordinated a database migration and
four deploys on one node by chat messages, and one deploy started inside a
window it had not heard about. A window is the record that replaces those
messages: an owner, a scope (what is being worked on), an expected end and a
note. Deploys and long jobs ask :func:`blocking` before they touch the node; a
window whose expected end has passed is still shown, marked overdue, and keeps
blocking until its owner closes it, because "probably finished" is the guess
this exists to remove.

Stored under the node's state directory (one file, rewritten atomically), so
every process on the node reads the same answer.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FILE = Path("node") / "windows.json"
#: What a window can cover. ``node`` covers everything on the machine.
SCOPES = ("node", "database", "serving", "storage", "models", "network", "deploy")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass
class Window:
    window_id: str
    owner: str
    scope: list[str]
    opened: float
    expected_end: float
    note: str = ""
    closed: float = 0.0
    closed_by: str = ""
    outcome: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def view(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        state = "closed" if self.closed else ("overdue" if now > self.expected_end else "open")
        return {
            "window_id": self.window_id,
            "owner": self.owner,
            "scope": self.scope,
            "note": self.note,
            "state": state,
            "opened_at": _iso(self.opened),
            "expected_end_at": _iso(self.expected_end),
            "remaining_min": None if self.closed else round((self.expected_end - now) / 60),
            "closed_at": _iso(self.closed) if self.closed else "",
            "closed_by": self.closed_by,
            "outcome": self.outcome,
        }


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else ""


def parse_duration(text: str) -> float:
    m = _DURATION.match(str(text or ""))
    if not m:
        raise ValueError(
            f"a duration is a number and a unit (s, m, h, d), like 45m or 2h; not {text!r}"
        )
    return float(m.group(1)) * _UNIT_S[m.group(2)]


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / FILE


def load(state_dir: Path) -> list[Window]:
    try:
        doc = json.loads(_path(state_dir).read_text())
    except (OSError, ValueError):
        return []
    out = []
    for w in doc.get("windows", []):
        try:
            out.append(Window(**w))
        except TypeError:
            continue
    return out


def _save(state_dir: Path, windows: list[Window], keep_closed_s: float = 7 * 86400) -> None:
    from axiom.infra.state_integrity import atomic_write_text

    now = time.time()
    kept = [w for w in windows if not w.closed or now - w.closed < keep_closed_s]
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        p, json.dumps({"windows": [asdict(w) for w in kept]}, sort_keys=True, indent=1)
    )


def _overlaps(a: list[str], b: list[str]) -> bool:
    return "node" in a or "node" in b or bool(set(a) & set(b))


def open_window(
    state_dir: Path, *, owner: str, scope: list[str], expect: str, note: str = ""
) -> Window:
    owner = (owner or "").strip()
    if not owner:
        raise ValueError("a window needs an owner: who to ask about it")
    scope = [s.strip() for s in scope if s.strip()] or ["node"]
    unknown = [s for s in scope if s not in SCOPES]
    if unknown:
        raise ValueError(f"unknown scope {', '.join(unknown)}; the scopes are {', '.join(SCOPES)}")
    now = time.time()
    windows = load(state_dir)
    for w in windows:
        if not w.closed and _overlaps(w.scope, scope):
            raise ValueError(
                f"window {w.window_id} ({w.owner}: {', '.join(w.scope)}) is already open on an overlapping scope until {_iso(w.expected_end)}; ask its owner, or wait for it to close"
            )
    w = Window(
        window_id=uuid.uuid4().hex[:8],
        owner=owner,
        scope=scope,
        opened=now,
        expected_end=now + parse_duration(expect),
        note=note,
    )
    windows.append(w)
    _save(state_dir, windows)
    return w


def extend(state_dir: Path, window_id: str, *, by: str, expect: str) -> Window:
    windows = load(state_dir)
    w = next((x for x in windows if x.window_id == window_id and not x.closed), None)
    if w is None:
        raise ValueError(f"no open window {window_id}")
    w.history.append({"at": time.time(), "by": by, "expected_end_was": w.expected_end})
    w.expected_end = time.time() + parse_duration(expect)
    _save(state_dir, windows)
    return w


def close_window(state_dir: Path, window_id: str, *, by: str, outcome: str = "") -> Window:
    windows = load(state_dir)
    w = next((x for x in windows if x.window_id == window_id and not x.closed), None)
    if w is None:
        raise ValueError(f"no open window {window_id}")
    w.closed, w.closed_by, w.outcome = time.time(), by, outcome
    _save(state_dir, windows)
    return w


def blocking(state_dir: Path, scope: list[str]) -> list[Window]:
    """Open (or overdue) windows a job on ``scope`` must not run inside."""
    want = [s for s in scope if s] or ["node"]
    return [w for w in load(state_dir) if not w.closed and _overlaps(w.scope, want)]


__all__ = [
    "FILE",
    "SCOPES",
    "Window",
    "blocking",
    "close_window",
    "extend",
    "load",
    "open_window",
    "parse_duration",
]
