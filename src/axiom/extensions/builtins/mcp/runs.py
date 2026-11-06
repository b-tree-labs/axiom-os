# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What each harness is actually running.

The MCP server is spawned per session over stdio, so an upgrade reaches a
harness the next time that harness launches and not before. Upgrade while an
editor is open and it keeps serving the old code: the tools are listed, the
version command reports the new number, and the behaviour is the old one. The
remedy is to restart the harness, and nothing anywhere said so, which is how
it came to be discovered by guessing.

Each start leaves a stamp naming the version it loaded. ``axi mcp status``
compares the stamps against what is installed now, so a harness that has not
restarted is named with its remedy rather than inferred.

**No process inspection, deliberately.** Liveness needs a different API per
platform and the colleagues who hit this are on Windows. The useful question
is not "is a process alive" but "the last time this harness started a server,
what did it get" — and a file answers that everywhere, with no privileges.

Everything here is an observation that rides in the server's startup and
first-request path, so every function swallows its own failures. A node root
that cannot be written must cost the server nothing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = [
    "Run",
    "anything_recorded",
    "current_commit",
    "installed_version",
    "is_behind",
    "note_client",
    "read_runs",
    "record_start",
    "runs_dir",
    "stale_runs",
]


#: A stamp this old names a harness nobody is using any more. Keeping it would
#: have `status` reporting a permanent complaint about an uninstalled editor,
#: and a complaint that cannot be resolved is one people learn to scroll past.
FORGET_AFTER = timedelta(days=30)

_UNATTRIBUTED = "unattributed"


@dataclass(frozen=True)
class Run:
    """One server start: what it loaded, when, and for whom."""

    version: str
    started_at: datetime
    pid: int
    client: str | None = None
    client_version: str | None = None
    #: The commit of the checkout the code was imported from, when it was one
    #: (ADR-202). An editable install keeps its version number while the
    #: checkout moves, so the version alone cannot say a server is behind.
    commit: str | None = None
    source: str | None = None

    @property
    def revision(self) -> str:
        """``0.70.2@9286bcac1`` for a checkout, ``0.70.2`` for a wheel."""
        return f"{self.version}@{self.commit[:9]}" if self.commit else self.version

    @property
    def harness(self) -> str:
        """What to call this in a sentence to a person.

        A start nobody has claimed is still worth printing — it means a server
        started and no harness has spoken to it yet — so it gets a name rather
        than being dropped for lacking one.
        """
        return self.client or "a harness that has not identified itself"


def _node_root(node_root: Path | str | None) -> Path:
    if node_root is not None:
        return Path(node_root)
    env = os.environ.get("AXIOM_HOME")
    if env:
        return Path(env)
    return Path(os.environ.get("HOME", ".")).expanduser() / ".axiom"


def runs_dir(*, node_root: Path | str | None = None) -> Path:
    """Where the stamps live. One file per harness."""
    return _node_root(node_root) / "mcp" / "runs"


def _slug(name: str) -> str:
    """A file name from a client name, which is whatever the client sent.

    Clients announce themselves with spaces, dots and slashes in their names,
    so this is narrowed to what is safe on every platform rather than trusted.
    """
    kept = [c if (c.isalnum() or c in "-_") else "-" for c in name.strip().lower()]
    slug = "".join(kept).strip("-") or _UNATTRIBUTED
    return slug[:64]


def installed_version() -> str | None:
    """The version installed right now, or ``None`` when it cannot be read."""
    try:
        from importlib.metadata import version

        return version("axiom-os-lm")
    except Exception:  # noqa: BLE001 — an unknown version is reported, not raised
        return None


def record_start(
    *,
    node_root: Path | str | None = None,
    version: str | None = None,
    pid: int | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Stamp this server start. Returns the path written, or ``None``.

    Keyed by pid until a client identifies itself, because at startup the
    server does not yet know who spawned it. :func:`note_client` moves the
    stamp under the client's name on the first request.
    """
    the_pid = os.getpid() if pid is None else pid
    try:
        d = runs_dir(node_root=node_root)
        d.mkdir(parents=True, exist_ok=True)
        commit, source = _loaded_commit_and_source()
        payload = {
            "version": version if version is not None else (installed_version() or "unknown"),
            "started_at": (now or datetime.now(UTC)).isoformat(),
            "pid": the_pid,
            "client": None,
            "client_version": None,
            "commit": commit,
            "source": source,
        }
        path = d / f"pid-{the_pid}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _prune(d)
        return path
    except Exception:  # noqa: BLE001 — an observation never breaks the server
        return None


def note_client(
    *,
    node_root: Path | str | None = None,
    pid: int | None = None,
    client: str | None,
    client_version: str | None = None,
) -> Path | None:
    """Attribute this run to the harness that just spoke to it.

    Collapses to one stamp per harness: a harness restarting twenty times a
    day leaves one file, holding its most recent start, which is the only one
    the question is about.
    """
    the_pid = os.getpid() if pid is None else pid
    if not client:
        return None
    try:
        d = runs_dir(node_root=node_root)
        unattributed = d / f"pid-{the_pid}.json"
        if not unattributed.exists():
            # Nothing stamped this start — a node root that was not writable
            # at startup, or a harness talking to a server we did not launch.
            return None
        payload = json.loads(unattributed.read_text(encoding="utf-8"))
        payload["client"] = client
        payload["client_version"] = client_version
        named = d / f"{_slug(client)}.json"
        named.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        unattributed.unlink(missing_ok=True)
        return named
    except Exception:  # noqa: BLE001
        return None


def _loaded_commit_and_source() -> tuple[str | None, str | None]:
    """What this process imported: the checkout commit (if any) and the source dir."""
    try:
        from axiom.infra.revision import loaded_revision

        loaded = loaded_revision()
        return loaded.commit, loaded.source
    except Exception:  # noqa: BLE001 — an unknown revision is recorded as unknown
        return None, None


def current_commit() -> str | None:
    """The commit of the checkout on disk now, or ``None`` for a wheel install."""
    try:
        from axiom.infra.revision import current_revision

        return current_revision().commit
    except Exception:  # noqa: BLE001
        return None


def _read_one(path: Path) -> Run | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Run(
            version=str(payload["version"]),
            started_at=datetime.fromisoformat(str(payload["started_at"])),
            pid=int(payload["pid"]),
            client=payload.get("client") or None,
            client_version=payload.get("client_version") or None,
            commit=payload.get("commit") or None,
            source=payload.get("source") or None,
        )
    except Exception:  # noqa: BLE001 — a half-written stamp is skipped, not fatal
        return None


def _prune(d: Path, *, now: datetime | None = None) -> None:
    cutoff = (now or datetime.now(UTC)) - FORGET_AFTER
    for path in d.glob("*.json"):
        run = _read_one(path)
        if run is None or run.started_at < cutoff:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def read_runs(*, node_root: Path | str | None = None) -> list[Run]:
    """Every harness's most recent start, newest first."""
    d = runs_dir(node_root=node_root)
    if not d.is_dir():
        return []
    found = [r for r in (_read_one(p) for p in sorted(d.glob("*.json"))) if r is not None]
    cutoff = datetime.now(UTC) - FORGET_AFTER
    found = [r for r in found if r.started_at >= cutoff]
    return sorted(found, key=lambda r: r.started_at, reverse=True)


def anything_recorded(*, node_root: Path | str | None = None) -> bool:
    """Has any start been stamped?

    Separate from an empty :func:`stale_runs` on purpose. Nothing recorded
    means the question is unanswerable; no stale runs means it was answered
    and the answer was no. A surface that renders both the same way tells
    somebody their harnesses are current when it has never seen one.
    """
    return bool(read_runs(node_root=node_root))


def is_behind(run: Run, *, installed: str, commit: str | None) -> bool:
    """Did *run* load something other than what is on disk now?

    A different version is behind. The same version at a different commit is
    behind too: an editable install keeps its number while the checkout moves,
    which is exactly how a server came to apply a rule replaced two weeks
    earlier. An unknown commit on either side is not a finding.
    """
    if run.version != installed:
        return True
    return bool(commit and run.commit and run.commit != commit)


def stale_runs(
    *,
    node_root: Path | str | None = None,
    installed: str | None = None,
    commit: str | None | object = "detect",
) -> list[Run]:
    """Harnesses whose last start loaded something other than what is
    installed now — which is to say, harnesses that need restarting.

    An unreadable installed version yields nothing rather than flagging
    everything: a comparison against an unknown is not a finding. ``commit``
    is the checkout commit on disk now; ``"detect"`` reads it, ``None`` skips
    the comparison.
    """
    current = installed if installed is not None else installed_version()
    if not current:
        return []
    now_commit = current_commit() if commit == "detect" else commit
    return [
        r
        for r in read_runs(node_root=node_root)
        if is_behind(r, installed=current, commit=now_commit)  # type: ignore[arg-type]
    ]
