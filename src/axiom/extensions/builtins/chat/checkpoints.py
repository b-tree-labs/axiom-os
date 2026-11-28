# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pre-image checkpoints for agent file writes: the substance behind ``/diff`` and ``/undo``.

Every file the agent writes (``write_file``, ``write_inbox_note``, the email draft tool) goes through
:func:`write_text`. When a chat turn is in progress (:func:`turn_binding`) it

1. snapshots the target's current bytes, or notes that it did not exist, into a content-hash-named blob
   under ``<state dir>/checkpoints/<session>/`` (directory 0700, blobs 0600),
2. stores the bytes about to be written the same way (so ``/diff`` can show exactly what the agent wrote
   even after the user edits the file), and
3. appends ONE record to the platform action ledger (``axiom.policy.action_ledger``): ``op_class``
   ``chat.file.write``, ``undo_ref`` ``checkpoint:<session>/<turn>/<n>``, and metadata carrying the
   hashes. The ledger is the only log; the directory holds nothing but content-addressed blobs. An undo
   is a ledger record too (``chat.file.undo`` / ``chat.file.redo``), so it is audited and reversible.

A turn is the unit of undo: every write made while one :func:`turn_binding` is open shares a turn id.

Bounded, never silently skipped: a file over ``MAX_FILE_BYTES``, or a session whose blobs would pass
``MAX_SESSION_BYTES``, makes the write REFUSE with an error that names the cap. Secrets are never
copied into the ledger: a snapshot of a file that matches a credential format is flagged by pattern name
only; the blob stays on the user's disk, owner-only.

What this cannot undo: shell commands and their side effects, network calls, and anything done by a tool
that does not write through :func:`write_text`.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The largest single file (pre-image or new content) that is checkpointed. Larger writes are refused.
MAX_FILE_BYTES = 8 * 1024 * 1024
#: The most blob data one session may hold. A write that would pass it is refused.
MAX_SESSION_BYTES = 256 * 1024 * 1024
#: The ledger returns at most this many records per query; a session past it is read truncated, and say so.
HISTORY_QUERY_LIMIT = 1000

OP_WRITE = "chat.file.write"
OP_UNDO = "chat.file.undo"
OP_REDO = "chat.file.redo"
KIND_WRITE, KIND_UNDO, KIND_REDO = "write", "undo", "redo"
_AGENT = "chat"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")

#: What a surface should say, verbatim, about the limits of undo.
CANNOT_UNDO = (
    "Cannot be undone: shell commands and their side effects, network calls, and anything done by a "
    "tool that does not write files through the agent's file tools. Only file contents are restored."
)


class SnapshotRefused(Exception):
    """A checkpoint could not be taken within the stated bounds; the operation must not proceed."""


# ---- identity of a turn ---------------------------------------------------------------------------


@dataclass
class _Binding:
    session_id: str
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    seq: int = 0


_current: contextvars.ContextVar[_Binding | None] = contextvars.ContextVar(
    "axiom_chat_checkpoint_turn", default=None
)


def _check_session_id(session_id: str) -> str:
    if not session_id or not _SAFE_ID.match(session_id) or set(session_id) == {"."}:
        raise ValueError(f"not a usable session id for checkpoints: {session_id!r}")
    return session_id


@contextlib.contextmanager
def turn_binding(session_id: str, *, strict: bool = True) -> Iterator[None]:
    """Mark the code inside as one agent turn of ``session_id``.

    ``strict=False`` (the chat loop) turns an unusable session id into "no checkpoints this turn" rather
    than an exception, so an odd id can never break a conversation.
    """
    try:
        _check_session_id(session_id)
    except ValueError:
        if strict:
            raise
        yield
        return
    token = _current.set(_Binding(session_id))
    try:
        yield
    finally:
        _current.reset(token)


# ---- the blob store -------------------------------------------------------------------------------


def _root(session_id: str) -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "checkpoints" / _check_session_id(session_id)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _store_bytes(root: Path, data: bytes) -> str:
    """Store ``data`` under its hash (0600), enforcing both caps. Returns the hash."""
    if len(data) > MAX_FILE_BYTES:
        raise SnapshotRefused(
            f"a file of {len(data)} bytes is over the {MAX_FILE_BYTES} bytes the checkpoint store keeps"
        )
    digest = _sha(data)
    blob = root / digest
    if blob.exists():
        return digest
    _mkdir_private(root)
    used = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
    if used + len(data) > MAX_SESSION_BYTES:
        raise SnapshotRefused(
            f"this session's checkpoints would pass the {MAX_SESSION_BYTES} bytes the store keeps "
            f"({used} used)"
        )
    tmp = root / f".{digest}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, blob)
    return digest


def read_blob(session_id: str, digest: str | None) -> bytes | None:
    """The bytes of a stored snapshot, ``None`` for "did not exist". ``FileNotFoundError`` if lost."""
    if digest is None:
        return None
    return (_root(session_id) / digest).read_bytes()


def _file_hash(path: Path) -> str | None:
    try:
        return _sha(path.read_bytes())
    except FileNotFoundError:
        return None


def current_hash(path: str | Path) -> str | None:
    """Hash of the file on disk now, ``None`` if it does not exist."""
    return _file_hash(Path(path))


def _secret_patterns(*blobs: bytes | None) -> list[str]:
    from axiom.infra.secret_scan import scan_bytes

    names: list[str] = []
    for data in blobs:
        if data:
            for finding in scan_bytes(data):
                if finding.pattern not in names:
                    names.append(finding.pattern)
    return names


# ---- the ledger ------------------------------------------------------------------------------------


def _ledger():
    from axiom.infra.paths import get_user_state_dir
    from axiom.policy.action_ledger import ActionLedger

    return ActionLedger(state_dir=get_user_state_dir())


def _record(
    binding: _Binding,
    *,
    op_class: str,
    tool: str,
    path: Path | str,
    kind: str,
    pre: str | None,
    post: str | None,
    outcome: str,
    secrets: list[str],
    reverts: list[str] | None = None,
    error: str = "",
) -> dict:
    binding.seq += 1
    metadata: dict[str, Any] = {
        "session": binding.session_id,
        "turn": binding.turn_id,
        "seq": binding.seq,
        "kind": kind,
        "pre": pre,
        "post": post,
    }
    if secrets:
        metadata["secrets"] = secrets
    if reverts:
        metadata["reverts"] = reverts
    if error:
        metadata["error"] = error[:300]
    return _ledger().record_action(
        agent=_AGENT,
        op_class=op_class,
        name=tool,
        candidate=str(path),
        guards=["checkpoint"],
        outcome=outcome,
        undo_ref=f"checkpoint:{binding.session_id}/{binding.turn_id}/{binding.seq}",
        metadata=metadata,
    )


# ---- the write seam ---------------------------------------------------------------------------------


def write_text(path: Path, content: str, *, tool: str) -> dict[str, Any]:
    """Write ``content`` to ``path`` (creating parents), checkpointed when a chat turn is open.

    Returns ``{}`` on success, ``{"checkpoint_note": ...}`` when something worth saying happened, or
    ``{"error": ...}`` when the checkpoint could not be taken and so the file was NOT written. With no
    turn open this is exactly ``mkdir`` + ``write_text``.
    """
    binding = _current.get()
    data = content.encode("utf-8")
    if binding is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {}

    root = _root(binding.session_id)
    try:
        before = path.read_bytes() if path.exists() else None
    except OSError as exc:
        return {"error": f"{path} was not written: its current contents could not be read to checkpoint ({exc})"}
    try:
        pre = None if before is None else _store_bytes(root, before)
        post = _store_bytes(root, data)
    except SnapshotRefused as exc:
        return {"error": f"{path} was not written: {exc}. Nothing was changed."}

    secrets = _secret_patterns(before, data)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except BaseException as exc:
        with contextlib.suppress(Exception):
            _record(
                binding, op_class=OP_WRITE, tool=tool, path=path, kind=KIND_WRITE, pre=pre, post=post,
                outcome="failed", secrets=secrets, error=f"{type(exc).__name__}: {exc}",
            )
        raise

    note: dict[str, Any] = {}
    try:
        _record(
            binding, op_class=OP_WRITE, tool=tool, path=path, kind=KIND_WRITE, pre=pre, post=post,
            outcome="proceeded", secrets=secrets,
        )
    except Exception as exc:  # noqa: BLE001 - the write happened; say that it is not undoable
        log.warning("checkpoint ledger record failed: %s", exc)
        return {"checkpoint_note": f"written, but NOT recorded for /undo: {type(exc).__name__}: {exc}"}
    if secrets:
        note["checkpoint_note"] = (
            f"the file matches credential format(s) ({', '.join(secrets)}); its snapshot is kept "
            f"owner-only under {root} and the contents are not logged"
        )
    return note


# ---- reading the history back -----------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    undo_ref: str
    path: str
    kind: str
    pre: str | None
    post: str | None
    seq: int
    ts: str
    secrets: tuple[str, ...] = ()


@dataclass
class Turn:
    number: int
    turn_id: str
    kind: str
    ts: str
    entries: list[Entry] = field(default_factory=list)
    reverts: list[str] = field(default_factory=list)
    reverted: bool = False

    @property
    def paths(self) -> list[str]:
        out: list[str] = []
        for e in self.entries:
            if e.path not in out:
                out.append(e.path)
        return out


def history(session_id: str) -> list[Turn]:
    """The session's successful checkpointed turns, oldest first, numbered 1..N.

    Read from the action ledger. ``Turn.reverted`` marks a turn an undo has reverted (and not since been
    redone).
    """
    prefix = f"checkpoint:{_check_session_id(session_id)}/"
    rows = _ledger().query(text=prefix, limit=HISTORY_QUERY_LIMIT)
    turns: dict[str, Turn] = {}
    order: list[Turn] = []
    for row in reversed(rows):
        md = row.get("metadata") or {}
        if not (row.get("undo_ref") or "").startswith(prefix) or row.get("outcome") != "proceeded":
            continue
        tid = md.get("turn")
        turn = turns.get(tid)
        if turn is None:
            turn = Turn(
                number=len(order) + 1, turn_id=tid, kind=md.get("kind", KIND_WRITE), ts=row["ts"],
                reverts=list(md.get("reverts") or []),
            )
            turns[tid] = turn
            order.append(turn)
        turn.entries.append(
            Entry(
                undo_ref=row["undo_ref"], path=row["candidate"], kind=md.get("kind", KIND_WRITE),
                pre=md.get("pre"), post=md.get("post"), seq=int(md.get("seq", 0)), ts=row["ts"],
                secrets=tuple(md.get("secrets") or ()),
            )
        )
    for turn in order:
        if turn.kind == KIND_UNDO:
            for tid in turn.reverts:
                if tid in turns:
                    turns[tid].reverted = True
        elif turn.kind == KIND_REDO:
            for tid in turn.reverts:
                if tid in turns:
                    undone = turns[tid]
                    undone.reverted = True  # an undo is reversible once
                    for original in undone.reverts:
                        if original in turns:
                            turns[original].reverted = False
    return order


def truncated(session_id: str) -> bool:
    """True if the ledger query hit its limit, i.e. older checkpoints of this session are not listed."""
    prefix = f"checkpoint:{_check_session_id(session_id)}/"
    return len(_ledger().query(text=prefix, limit=HISTORY_QUERY_LIMIT)) >= HISTORY_QUERY_LIMIT


def undoable(turns: list[Turn]) -> list[Turn]:
    """Turns ``/undo`` may revert, newest first: writes and undos not already reverted (a redo is final)."""
    return [t for t in reversed(turns) if t.kind in (KIND_WRITE, KIND_UNDO) and not t.reverted]


# ---- undo --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Conflict:
    path: str
    expected: str | None  # what the agent left there
    actual: str | None  # what is there now


@dataclass
class UndoResult:
    restored: list[str]
    removed: list[str]
    turn_numbers: list[int]
    redo_of: bool = False


def plan_conflicts(session_id: str, turns: list[Turn]) -> tuple[dict[str, tuple[str | None, str | None]], list[Conflict]]:
    """For reverting ``turns``: ``{path: (target_hash, expected_hash)}`` and the files changed since.

    Per path across the turns (in turn order): the target is the FIRST pre-image, the expected current
    state is the LAST post. A file whose content now differs was changed after the agent wrote it.
    """
    ordered = sorted(turns, key=lambda t: t.number)
    targets: dict[str, tuple[str | None, str | None]] = {}
    for turn in ordered:
        for e in turn.entries:
            first = targets.get(e.path, (e.pre, None))[0]
            targets[e.path] = (first, e.post)
    conflicts = [
        Conflict(path, expected, actual)
        for path, (_, expected) in targets.items()
        if (actual := current_hash(path)) != expected
    ]
    return targets, conflicts


def undo(session_id: str, turns: list[Turn], *, tool: str = "undo") -> UndoResult:
    """Revert ``turns`` (already checked for conflicts, or forced). One audited ledger record per file.

    Order: (1) verify every needed snapshot exists, (2) snapshot the CURRENT contents of every file
    (the forward snapshot, so the undo can be reversed and a forced undo loses nothing), (3) restore,
    (4) ledger. A refusal in (1)/(2) changes nothing.
    """
    targets, _ = plan_conflicts(session_id, turns)
    root = _root(session_id)
    redo_of = all(t.kind == KIND_UNDO for t in turns)
    for path, (target, _) in targets.items():
        if target is not None and not (root / target).exists():
            raise SnapshotRefused(f"the snapshot of {path} is missing from {root}; nothing was changed")
    forward: dict[str, str | None] = {}
    for path in targets:
        p = Path(path)
        forward[path] = _store_bytes(root, p.read_bytes()) if p.exists() else None
    secrets = {p: _secret_patterns(read_blob(session_id, h)) for p, h in forward.items()}

    binding = _Binding(session_id)
    kind, op = (KIND_REDO, OP_REDO) if redo_of else (KIND_UNDO, OP_UNDO)
    restored: list[str] = []
    removed: list[str] = []
    for path, (target, _) in targets.items():
        p = Path(path)
        if target is None:
            p.unlink(missing_ok=True)
            removed.append(path)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes((root / target).read_bytes())
            restored.append(path)
        _record(
            binding, op_class=op, tool=tool, path=path, kind=kind, pre=forward[path], post=target,
            outcome="proceeded", secrets=secrets[path], reverts=[t.turn_id for t in turns],
        )
    return UndoResult(restored, removed, [t.number for t in turns], redo_of)
