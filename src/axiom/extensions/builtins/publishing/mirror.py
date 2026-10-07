# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Single-document mirroring between a remote editor and a local file.

The person edits wherever they please — a web editor, a comment link, a
phone. The agent edits the local file. This module makes the coordination
explicit instead of accidental:

- One side is canonical per mode. In ``web-canonical`` (the default and the
  only P0 mode), the remote editor is the source of truth and the local
  mirror follows; local edits are *pushed* to the remote with an expected
  version, never written blind.
- Every write carries the version it expects. A foreign version in between
  aborts the push, re-reads, and reports — whoever wrote concurrently is
  never silently overwritten, in either direction.
- A remote save whose content equals an OLD local base is a stale editor
  session flushing its buffer, not new work. It is repaired (the last good
  text pushed back) and attributed, rather than followed.
- Both sides changed with different content: hold and report. The local
  edit is preserved beside the mirror as a conflict copy; nothing is lost,
  nothing is guessed.
- When the mirror lives in a git repository and annotation is enabled,
  every sync event becomes a commit with provenance trailers
  (``Synced-From``, ``Endpoint-Version``), so git operations see annotated
  history rather than mystery edits.

The remote side is any :class:`RemoteEditorEndpoint`. The engine never
imports a concrete endpoint; transports are injected, which is also what
makes the suite runnable with only the standard library.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from axiom.infra.conflict import ConflictOutcome
from axiom.infra.git import ensure_managed_gitignore


class VersionConflict(Exception):
    """A write expected one remote version and found another."""

    def __init__(self, expected: str, found: str):
        super().__init__(f"expected remote version {expected}, found {found}")
        self.expected = expected
        self.found = found


@dataclass(frozen=True)
class RemoteDoc:
    """One remote read: the text, its version label, and who wrote it."""

    text: str
    version: str
    author_app: str = ""


@dataclass(frozen=True)
class StaleFlushDetected:
    """A remote save matched an old base — a stale session, not new work."""

    version: str
    author_app: str
    matched_base_hash: str


@dataclass
class SyncReport:
    """What one reconcile or watch pass did."""

    action: str  # pull | push | conflict | blocked | resolved | noop | repair
    remote_version: str = ""
    git_commit: str | None = None
    conflict_copy: str | None = None
    conflict: ConflictOutcome | None = None
    detected: StaleFlushDetected | None = None
    notes: list[str] = field(default_factory=list)


class RemoteEditorEndpoint(Protocol):
    """The remote side: read current, write with expectation, list versions."""

    def read(self) -> RemoteDoc: ...

    def write(self, text: str, expected_version: str) -> str: ...

    def versions(self, limit: int = 10) -> list[RemoteDoc]: ...


# The mirror's working artifacts, ignored via a managed .gitignore block
# when the mirror lives in a git repo (ADR-112 §D5).
_GITIGNORE_MARKER = "axiom mirror"
_GITIGNORE_PATTERNS = [
    "*.conflict",
    "*.conflict.*",
    "*.mirrormeta.json",
    ".axi/publisher/mirror-state/",
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically.

    A concurrent reader — another engine, in the two-processes-on-one-mirror
    case — or a crash mid-write sees either the whole previous file or the whole
    new one, never a torn partial. That is what makes ``state.json`` safe to read
    while another engine is writing it, and what makes a crash non-destructive.
    The temp file is created in the target's own directory so the rename is a
    true atomic rename on a single filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class MirrorEngine:
    """Coordinate one document between a remote editor and a local mirror."""

    def __init__(
        self,
        *,
        endpoint: RemoteEditorEndpoint,
        mirror_path: Path,
        state_path: Path,
        git_annotate: bool = False,
        base_history: int = 20,
    ):
        self.endpoint = endpoint
        self.mirror_path = Path(mirror_path)
        self.state_path = Path(state_path)
        self.git_annotate = git_annotate
        self.base_history = base_history
        self._state = self._load_state()

    # -- state ---------------------------------------------------------------

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"base_hash": None, "base_text": None, "remote_version": None,
                "old_base_hashes": []}

    def _save_state(self) -> None:
        _atomic_write_bytes(
            self.state_path, json.dumps(self._state, indent=2).encode("utf-8"))

    def _set_base(self, text: str, version: str) -> None:
        old = self._state.get("base_hash")
        if old:
            hashes = self._state.setdefault("old_base_hashes", [])
            if old not in hashes:
                hashes.append(old)
            del hashes[: -self.base_history]
        self._state["base_hash"] = _sha(text)
        self._state["base_text"] = text
        self._state["remote_version"] = version
        self._save_state()

    # -- git -----------------------------------------------------------------

    def _in_git_repo(self) -> bool:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=self.mirror_path.parent, capture_output=True, text=True,
        )
        return probe.returncode == 0 and probe.stdout.strip() == "true"

    def ensure_gitignore(self) -> bool:
        """Maintain the managed .gitignore block for the mirror's artifacts
        when it lives in a git repo (ADR-112 §D5). Idempotent; returns whether
        the file changed. Best-effort — a git failure never blocks a sync."""
        if not self._in_git_repo():
            return False
        try:
            _, changed = ensure_managed_gitignore(
                self.mirror_path.parent, marker=_GITIGNORE_MARKER,
                patterns=_GITIGNORE_PATTERNS)
            return changed
        except Exception:  # noqa: BLE001 — hygiene must never break a sync
            return False

    def _git_commit(self, action: str, version: str) -> str | None:
        if not (self.git_annotate and self._in_git_repo()):
            return None
        if self._state.get("conflict"):
            return None  # defer: never commit while a conflict is unresolved
        cwd = self.mirror_path.parent
        subprocess.run(["git", "add", "--", str(self.mirror_path.name)],
                       cwd=cwd, check=True, capture_output=True)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=cwd)
        if staged.returncode == 0:
            return None  # nothing actually changed
        message = (
            f"mirror: {action} {self.mirror_path.name}\n\n"
            f"Synced-From: remote editor endpoint\n"
            f"Endpoint-Version: {version}\n"
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", message, "--",
             str(self.mirror_path.name)],
            cwd=cwd, check=True, capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                             capture_output=True, text=True, check=True)
        return sha.stdout.strip()

    # -- core ----------------------------------------------------------------

    def _mirror_text(self) -> str | None:
        # bytes-faithful: read_text() would translate CRLF to LF and make a
        # normalizing remote look like an eternal local edit
        if not self.mirror_path.exists():
            return None
        return self.mirror_path.read_bytes().decode("utf-8")

    def _preserve_local(self, text: str) -> Path:
        """Write ``text`` to a conflict copy that never clobbers an earlier
        one: ``doc.md.conflict``, then ``doc.md.conflict.1``, and so on."""
        candidate = self.mirror_path.with_suffix(self.mirror_path.suffix + ".conflict")
        counter = 0
        while (candidate.exists()
               and candidate.read_bytes().decode("utf-8") != text):
            counter += 1
            candidate = self.mirror_path.with_suffix(
                self.mirror_path.suffix + f".conflict.{counter}")
        _atomic_write_bytes(candidate, text.encode("utf-8"))
        return candidate

    def _enter_conflict(self, local: str, remote: RemoteDoc, *,
                        reason: str) -> SyncReport:
        """Record a conflict and block sync until a human resolves it.

        The incoming canonical text lands in the mirror so the live file stays
        readable — never conflict markers, which the watcher would push straight
        back to the remote and corrupt it. Your local edit is preserved in a
        ``.conflict`` sidecar; the blocked state is written to the state file.
        Every later pass short-circuits to a ``blocked`` report until
        :meth:`resolve` clears it. This is git's essence — conflict is explicit,
        both sides are kept, and nothing auto-proceeds — made safe for a live,
        byte-faithful, continuously synced document.
        """
        self.ensure_gitignore()  # the .conflict sidecar should be ignored
        sidecar = self._preserve_local(local)
        _atomic_write_bytes(self.mirror_path, remote.text.encode("utf-8"))
        outcome = ConflictOutcome(
            resource=self.mirror_path.name, reason=reason,
            preserved_path=str(sidecar), incoming_version=remote.version,
            blocked=True)
        self._state["conflict"] = {
            "sidecar": str(sidecar), "incoming_version": remote.version,
            "your_hash": _sha(local), "reason": reason}
        # _set_base persists the state; it sets only base fields, so the
        # conflict key we just wrote survives the save.
        self._set_base(remote.text, remote.version)
        return SyncReport(action="conflict", remote_version=remote.version,
                          conflict_copy=str(sidecar), conflict=outcome,
                          notes=[reason])

    def _blocked_report(self) -> SyncReport:
        """The report every pass returns while a conflict is unresolved."""
        c = self._state["conflict"]
        outcome = ConflictOutcome(
            resource=self.mirror_path.name,
            reason=c.get("reason", "unresolved conflict"),
            preserved_path=c.get("sidecar"),
            incoming_version=c.get("incoming_version", ""), blocked=True)
        return SyncReport(
            action="blocked", remote_version=c.get("incoming_version", ""),
            conflict_copy=c.get("sidecar"), conflict=outcome,
            notes=["sync is blocked by an unresolved conflict; run "
                   "`mirror resolve <name> --theirs|--ours|--merged`"])

    def _clear_conflict(self, *, remove_sidecar: bool = True) -> None:
        c = self._state.pop("conflict", None)
        self._save_state()
        if remove_sidecar and c and c.get("sidecar"):
            try:
                Path(c["sidecar"]).unlink()
            except OSError:
                pass

    def _pull(self, remote: RemoteDoc) -> SyncReport:
        _atomic_write_bytes(self.mirror_path, remote.text.encode("utf-8"))
        self._set_base(remote.text, remote.version)
        commit = self._git_commit("pull", remote.version)
        return SyncReport(action="pull", remote_version=remote.version,
                          git_commit=commit)

    def reconcile(self) -> SyncReport:
        """One directional pass: decide who moved, and follow the doctrine."""
        if self._state.get("conflict"):
            return self._blocked_report()
        remote = self.endpoint.read()
        local = self._mirror_text()
        base_hash = self._state.get("base_hash")

        if local is None:
            return self._pull(remote)
        if base_hash is None:
            # no base to judge by (fresh or damaged state): identical content
            # re-baselines quietly; different content is preserved, never
            # overwritten by a re-baselining pull
            if local == remote.text:
                self._set_base(local, remote.version)
                return SyncReport(action="noop", remote_version=remote.version,
                                  notes=["re-baselined from matching content"])
            return self._enter_conflict(
                local, remote,
                reason="state was missing; local content preserved")

        remote_moved = _sha(remote.text) != base_hash
        local_moved = _sha(local) != base_hash

        if not remote_moved and not local_moved:
            return SyncReport(action="noop", remote_version=remote.version)

        if remote_moved and not local_moved:
            return self._pull(remote)

        if local_moved and not remote_moved:
            try:
                new_version = self.endpoint.write(local, remote.version)
            except VersionConflict:
                # someone wrote between our read and our write: re-read and
                # let the both-moved path decide — never overwrite them
                return self.reconcile()
            self._set_base(local, new_version)
            commit = self._git_commit("push", new_version)
            return SyncReport(action="push", remote_version=new_version,
                              git_commit=commit)

        # both moved
        if remote.text == local:
            self._set_base(local, remote.version)
            return SyncReport(action="noop", remote_version=remote.version,
                              notes=["both sides converged identically"])
        return self._enter_conflict(
            local, remote, reason="both sides changed with different content")

    def watch_once(self) -> SyncReport:
        """Reconcile, but first check for a stale-session flush and repair it.

        A stale flush is a remote save whose content hashes to an OLD base —
        text this mirror has already moved past. Following it would resurrect
        a dead buffer; instead the last good text is pushed back and the
        flush is attributed to the application that wrote it.
        """
        if self._state.get("conflict"):
            return self._blocked_report()
        remote = self.endpoint.read()
        old_hashes = set(self._state.get("old_base_hashes", []))
        current_base = self._state.get("base_hash")
        remote_hash = _sha(remote.text)

        good = self._state.get("base_text")
        base_text_intact = bool(good) and _sha(good) == current_base
        if (current_base and remote_hash != current_base
                and remote_hash in old_hashes and base_text_intact):
            # repair-once rule: the same old text returning after a repair
            # is either a human insisting on a revert or a zombie repeating —
            # indistinguishable on content. Never war and never silently
            # follow: suspend repair, follow the remote, preserve the good
            # text beside the mirror, and say so in the report.
            if self._state.get("repair_hash") == remote_hash:
                preserved = self._preserve_local(good)
                self._state["repair_hash"] = None
                self._save_state()
                report = self._pull(remote)
                report.conflict_copy = str(preserved)
                report.conflict = ConflictOutcome(
                    resource=self.mirror_path.name,
                    reason="stale text returned after a repair; followed the "
                           "remote and preserved your last good text",
                    preserved_path=str(preserved),
                    incoming_version=remote.version, blocked=False,
                    winner="remote")
                report.notes.append(
                    "repair suspended: this text returned after a repair; "
                    "if a stale pane wrote it, restore from the conflict copy")
                return report
            try:
                new_version = self.endpoint.write(good, remote.version)
            except VersionConflict:
                return self.reconcile()
            self._state["remote_version"] = new_version
            self._state["repair_hash"] = remote_hash
            self._save_state()
            commit = self._git_commit("repair", new_version)
            return SyncReport(
                action="repair", remote_version=new_version, git_commit=commit,
                detected=StaleFlushDetected(version=remote.version,
                                            author_app=remote.author_app,
                                            matched_base_hash=remote_hash),
                notes=["stale session flush repaired"])
        if self._state.get("repair_hash") and remote_hash == current_base:
            self._state["repair_hash"] = None
            self._save_state()
        return self.reconcile()

    def resolve(self, strategy: str) -> SyncReport:
        """Clear a blocked conflict by choosing a side (ADR-112 §D3).

        - ``theirs`` — keep the incoming remote text already in the mirror.
        - ``ours``   — restore your preserved edit and push it to the remote.
        - ``merged`` — push whatever the mirror now holds (you edited it by hand
          into a merge of both sides).

        If the remote advanced again since the conflict, the push is refused and
        the conflict re-opens against the *new* remote rather than clobbering it —
        your text is preserved, never lost.
        """
        if strategy not in ("theirs", "ours", "merged"):
            raise ValueError(
                f"unknown resolution {strategy!r}; choose theirs, ours, or merged")
        c = self._state.get("conflict")
        if not c:
            return SyncReport(action="noop", notes=["no conflict to resolve"])

        if strategy == "theirs":
            # the incoming text is already in the mirror and is the base
            self._clear_conflict(remove_sidecar=True)
            return SyncReport(
                action="resolved",
                remote_version=self._state.get("remote_version", ""),
                notes=["kept the remote version (theirs)"])

        # ours / merged both push the local file up to the canonical remote
        if strategy == "ours":
            sidecar = c.get("sidecar")
            if not sidecar or not Path(sidecar).exists():
                return SyncReport(action="noop", notes=[
                    "no preserved copy to restore; edit the mirror and use "
                    "--merged"])
            _atomic_write_bytes(self.mirror_path, Path(sidecar).read_bytes())

        text = self._mirror_text() or ""
        # push against the version we saw AT CONFLICT TIME, not a fresh read: a
        # human resolves based on the state they were shown, so a remote that
        # moved on since must re-block rather than be overwritten blind.
        expected = c.get("incoming_version") or self._state.get("remote_version", "")
        try:
            new_version = self.endpoint.write(text, expected)
        except VersionConflict:
            # remote advanced since the conflict: re-open against the fresh
            # remote, keeping the text we were about to push as the preserved side
            self._clear_conflict(remove_sidecar=True)
            fresh = self.endpoint.read()
            return self._enter_conflict(
                text, fresh,
                reason="remote advanced during resolve; still conflicted")
        self._set_base(text, new_version)
        self._clear_conflict(remove_sidecar=True)
        commit = self._git_commit("push", new_version)
        label = "merged" if strategy == "merged" else "local (ours)"
        return SyncReport(action="resolved", remote_version=new_version,
                          git_commit=commit, notes=[f"pushed your {label} version"])
