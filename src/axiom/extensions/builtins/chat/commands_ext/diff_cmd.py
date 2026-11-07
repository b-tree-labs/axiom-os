# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/diff``: what the agent changed in this session, against the pre-images it kept (C15).

Not a git diff: it reads the checkpoints ``checkpoints.write_text`` took, so it works in any directory and
shows the agent's own writes even where the user has since edited the file. Grouped by turn; bounded.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from .. import checkpoints
from ..command_registry import command

#: Lines of diff shown before the output is cut; ``--full`` lifts it.
DIFF_MAX_LINES = 200

USAGE = "\n  Usage: /diff [path] [--turn N] [--full]\n"


def _lines(data: bytes | None) -> list[str] | None:
    if data is None:
        return []
    try:
        return data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def _mask(line: str) -> str:
    from axiom.infra.secret_scan import scan_text

    found = scan_text(line)
    if not found:
        return line
    names = ", ".join(sorted({f.pattern for f in found}))
    return f"[line withheld: matches credential format {names}]\n"


def _entry_diff(session_id: str, entry: checkpoints.Entry) -> list[str]:
    try:
        pre = checkpoints.read_blob(session_id, entry.pre)
        post = checkpoints.read_blob(session_id, entry.post)
    except FileNotFoundError:
        return [f"  {entry.path}: snapshot missing from the checkpoint store\n"]
    a, b = _lines(pre), _lines(post)
    if a is None or b is None:
        return [
            f"  {entry.path}: binary file, "
            f"{'did not exist' if pre is None else f'{len(pre)} bytes'} -> {len(post or b'')} bytes\n"
        ]
    from_name = f"{entry.path} (did not exist)" if entry.pre is None else f"{entry.path} (before)"
    body = [
        ln if ln.endswith("\n") else ln + "\n"
        for ln in difflib.unified_diff(a, b, fromfile=from_name, tofile=f"{entry.path} (after)")
    ]
    return [ln if i < 2 else _mask(ln) for i, ln in enumerate(body)]


def _matches(entry_path: str, wanted: str) -> bool:
    if entry_path == wanted or entry_path.endswith(wanted):
        return True
    try:
        return Path(entry_path).resolve() == Path(wanted).resolve()
    except OSError:
        return False


def render(session_id: str, *, path: str = "", turn: int | None = None, full: bool = False) -> str:
    turns = checkpoints.history(session_id)
    if turn is not None and not any(t.number == turn for t in turns):
        return f"\n  There is no turn {turn} with file changes in this session (1..{len(turns)}).\n"
    out: list[str] = []
    for t in turns:
        if turn is not None and t.number != turn:
            continue
        entries = [e for e in t.entries if not path or _matches(e.path, path)]
        if not entries:
            continue
        label = {"write": "", "undo": " (an /undo)", "redo": " (undoing an /undo)"}[t.kind]
        out.append(f"Turn {t.number}{label}: {len(entries)} file(s)\n")
        if t.reverted:
            out.append("  note: this turn was reverted by /undo\n")
        for e in entries:
            out.extend(_entry_diff(session_id, e))
            if t.kind == "write" and not t.reverted and checkpoints.current_hash(e.path) != e.post:
                out.append(
                    f"  note: {e.path} has changed since the agent wrote it; "
                    "this shows the agent's write, not the file as it is now\n"
                )
        out.append("\n")
    if not out:
        what = f" for {path}" if path else ""
        return f"\n  No file changes by the agent in this session{what}.\n"
    if checkpoints.truncated(session_id):
        out.insert(0, f"(only the most recent {checkpoints.HISTORY_QUERY_LIMIT} checkpoint records are listed)\n")
    if not full and len(out) and sum(x.count("\n") for x in out) > DIFF_MAX_LINES:
        shown: list[str] = []
        budget = DIFF_MAX_LINES
        for chunk in out:
            for ln in chunk.splitlines(keepends=True):
                if budget == 0:
                    break
                shown.append(ln)
                budget -= 1
        rest = sum(x.count("\n") for x in out) - DIFF_MAX_LINES
        shown.append(
            f"\n... {rest} more lines not shown (limit {DIFF_MAX_LINES} lines). "
            "/diff --full shows everything; /diff --turn N shows one turn.\n"
        )
        out = shown
    return "\n" + "".join("  " + ln if ln.strip() else ln for ln in out)


def _diff(ctx, args: list[str]) -> str:
    path, turn, full = "", None, False
    it = iter(args)
    for a in it:
        if a == "--full":
            full = True
        elif a == "--turn":
            try:
                turn = int(next(it))
            except (StopIteration, ValueError):
                return USAGE
        elif a.startswith("--"):
            return USAGE
        elif not path:
            path = a
        else:
            return USAGE
    return render(ctx.agent.session.session_id, path=path, turn=turn, full=full)


command(
    "/diff",
    "What the agent changed this session (works outside git)",
    "Agent",
    usage="/diff [path] [--turn N] [--full]",
)(_diff)
