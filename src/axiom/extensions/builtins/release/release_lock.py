# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The release lock: nothing lands between "bump opened" and "tag pushed".

A release needs one green run on the head it tags, and every merge in that
window replaces the pending main run and adds one more the next merge can
supersede. On 2026-11-10 a person held that window by hand across four
sessions (#1782). This module holds it in code.

The lock is two things, and the second is optional:

* **A small state file**, ``<state>/agents/rivet/release-lock.json``, on the
  machine that took it. It remembers what to re-arm.
* **A repo-level marker**: a GitHub Actions repository variable named
  ``AXIOM_RELEASE_LOCK`` whose value is the lock as JSON (reason, holder,
  expiry, the pull requests disarmed). Any machine's lander reads it, so a lock
  taken in one session is honoured in every other. A variable rather than a
  label on the bump pull request, because the bump path does not always open a
  pull request and a variable survives either way. Deleting the variable is
  what lifts the lock for everyone; whoever deletes it is the one who re-arms.

While the lock is held the land skill arms nothing and refuses to merge. Any
pull request it finds armed it disarms (``gh pr merge --disable-auto``), with a
comment naming the lock and when it lifts, and records it on the lock. On the
tag, or after the timeout (30 minutes by default), the lock lifts: every
recorded pull request is re-armed at the head it had, with a comment, and
HERALD hears about both edges when a roster is configured.

Nothing here force-pushes or merges. The only forge writes are the variable,
``--disable-auto`` / ``--auto`` and comments.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from axiom.extensions.builtins.release.ci_health.alerts import Alert, send_alerts
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.infra.state import locked_append_jsonl

LOCK_VARIABLE = "AXIOM_RELEASE_LOCK"
DEFAULT_MINUTES = 30
_METHODS = frozenset({"merge", "squash", "rebase"})


class ForgeWriteFailed(RuntimeError):
    """A forge call failed. The caller records it and goes on."""


class LockHeld(LanderRefused):
    """Another release holds the lock."""

    def __init__(self, lock: ReleaseLock) -> None:
        super().__init__(
            f"release lock already held on {lock.repo}: {lock.reason} ({lock.held_by}); "
            f"lifts at {lock.lifts_at()} or on tag"
        )
        self.lock = lock


def default_holder() -> str:
    try:
        return f"@{getpass.getuser()}"
    except Exception:  # noqa: BLE001 - a nameless holder is still a holder
        return "@unknown"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class DisarmedPull:
    number: int
    head_sha: str
    merge_method: str = "merge"
    disarmed_at: str = ""
    rearmed: str = ""


@dataclass
class ReleaseLock:
    repo: str
    reason: str
    held_by: str
    taken_at: str
    expires_at: str
    marker: str = ""
    marker_error: str = ""
    bump_pr: int = 0
    disarmed: list[DisarmedPull] = field(default_factory=list)
    announced: bool = False

    def expired(self, now: datetime) -> bool:
        return now >= _parse(self.expires_at)

    def minutes_left(self, now: datetime) -> int:
        return max(0, int((_parse(self.expires_at) - now).total_seconds() // 60))

    def lifts_at(self) -> str:
        return _parse(self.expires_at).strftime("%H:%MZ")

    def describe(self) -> str:
        return (
            f"release lock on {self.repo} held by {self.held_by} ({self.reason}); "
            f"lifts at {self.lifts_at()} or on tag"
        )

    def payload(self) -> str:
        """The marker's value: the lock without machine-local fields."""
        data = self.to_dict()
        data.pop("marker", None)
        data.pop("marker_error", None)
        return json.dumps(data, sort_keys=True)

    def to_dict(self) -> dict:
        return {**asdict(self), "disarmed": [asdict(item) for item in self.disarmed]}

    @classmethod
    def from_dict(cls, data: dict) -> ReleaseLock:
        rows = data.get("disarmed") or []
        return cls(
            repo=str(data.get("repo") or ""),
            reason=str(data.get("reason") or ""),
            held_by=str(data.get("held_by") or ""),
            taken_at=str(data.get("taken_at") or ""),
            expires_at=str(data.get("expires_at") or ""),
            marker=str(data.get("marker") or ""),
            marker_error=str(data.get("marker_error") or ""),
            bump_pr=int(data.get("bump_pr") or 0),
            disarmed=[
                DisarmedPull(
                    number=int(row.get("number") or 0),
                    head_sha=str(row.get("head_sha") or ""),
                    merge_method=str(row.get("merge_method") or "merge"),
                    disarmed_at=str(row.get("disarmed_at") or ""),
                    rearmed=str(row.get("rearmed") or ""),
                )
                for row in rows
                if isinstance(row, dict) and row.get("number")
            ],
            announced=bool(data.get("announced")),
        )


# -- the forge seam ----------------------------------------------------------


class LockForge(Protocol):
    def get_variable(self, repo: str, name: str) -> str | None: ...

    def set_variable(self, repo: str, name: str, value: str) -> None: ...

    def delete_variable(self, repo: str, name: str) -> bool: ...

    def armed_pulls(self, repo: str) -> list[dict]: ...

    def disarm(self, repo: str, number: int) -> None: ...

    def arm(self, repo: str, number: int, head_sha: str, method: str) -> None: ...

    def comment(self, repo: str, number: int, body: str) -> None: ...


class NullLockForge:
    """No forge: the lock is the state file alone."""

    def get_variable(self, repo: str, name: str) -> str | None:
        return None

    def set_variable(self, repo: str, name: str, value: str) -> None:
        raise ForgeWriteFailed("no forge for this repository")

    def delete_variable(self, repo: str, name: str) -> bool:
        return False

    def armed_pulls(self, repo: str) -> list[dict]:
        return []

    def disarm(self, repo: str, number: int) -> None:
        return None

    def arm(self, repo: str, number: int, head_sha: str, method: str) -> None:
        return None

    def comment(self, repo: str, number: int, body: str) -> None:
        return None


def _refuse_force(args: list[str]) -> None:
    for arg in args:
        if arg in {"--force", "-f", "--admin"} or arg.startswith("--force") or arg.startswith("+"):
            raise ForgeWriteFailed("force flags are not allowed here")


def _default_gh(args: list[str]) -> str:
    _refuse_force(args)
    try:
        done = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForgeWriteFailed(f"gh {args[0] if args else ''}: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise ForgeWriteFailed(f"gh {' '.join(args[:2])}: {detail[-1] if detail else done.returncode}")
    return done.stdout


class GhLockForge:
    """The GitHub implementation, through ``gh``. The token stays in the environment."""

    def __init__(self, repo: str, runner=None) -> None:
        self.repo = repo
        self._run = runner or _default_gh

    def get_variable(self, repo: str, name: str) -> str | None:
        if name in os.environ:
            # A workflow hands the variable to the step (`vars.<name>`). Its token
            # is not guaranteed to read the variables API, and a failed read
            # below means "no lock", so what was handed in is what is believed.
            return os.environ[name] or None
        try:
            raw = self._run(["api", f"repos/{repo}/actions/variables/{name}"])
        except ForgeWriteFailed:
            return None
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return None
        value = data.get("value") if isinstance(data, dict) else None
        return str(value) if value else None

    def set_variable(self, repo: str, name: str, value: str) -> None:
        try:
            self._run(["api", "-X", "POST", f"repos/{repo}/actions/variables", "-f", f"name={name}", "-f", f"value={value}"])
        except ForgeWriteFailed:
            self._run(["api", "-X", "PATCH", f"repos/{repo}/actions/variables/{name}", "-f", f"value={value}"])
        if name in os.environ:
            os.environ[name] = value

    def delete_variable(self, repo: str, name: str) -> bool:
        try:
            self._run(["api", "-X", "DELETE", f"repos/{repo}/actions/variables/{name}"])
        except ForgeWriteFailed:
            return False
        if name in os.environ:
            os.environ[name] = ""
        return True

    def armed_pulls(self, repo: str) -> list[dict]:
        raw = self._run(
            ["pr", "list", "--repo", repo, "--state", "open", "--limit", "100", "--json", "number,headRefOid,autoMergeRequest"]
        )
        try:
            rows = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise ForgeWriteFailed("pull request list was not JSON") from exc
        found: list[dict] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("autoMergeRequest"):
                continue
            request = row["autoMergeRequest"] if isinstance(row["autoMergeRequest"], dict) else {}
            found.append(
                {
                    "number": int(row.get("number") or 0),
                    "head_sha": str(row.get("headRefOid") or ""),
                    "merge_method": str(request.get("mergeMethod") or "MERGE"),
                }
            )
        return [row for row in found if row["number"]]

    def disarm(self, repo: str, number: int) -> None:
        self._run(["pr", "merge", str(int(number)), "--repo", repo, "--disable-auto"])

    def arm(self, repo: str, number: int, head_sha: str, method: str) -> None:
        flag = method.lower() if method.lower() in _METHODS else "merge"
        if not head_sha:
            raise ForgeWriteFailed(f"#{number}: no head recorded to re-arm against")
        self._run(["pr", "merge", str(int(number)), "--repo", repo, "--auto", f"--{flag}", "--match-head-commit", head_sha])

    def comment(self, repo: str, number: int, body: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
            handle.write(body)
            path = handle.name
        try:
            self._run(["pr", "comment", str(int(number)), "--repo", repo, "--body-file", path])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# -- state ---------------------------------------------------------------------


def lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / "agents" / "rivet" / "release-lock.json"


def history_path(state_dir: Path) -> Path:
    return Path(state_dir) / "agents" / "rivet" / "release-lock.jsonl"


def load(state_dir: Path) -> ReleaseLock | None:
    path = lock_path(state_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return ReleaseLock.from_dict(data) if isinstance(data, dict) and data.get("repo") else None


def save(state_dir: Path, lock: ReleaseLock) -> None:
    path = lock_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def clear(state_dir: Path) -> None:
    try:
        lock_path(state_dir).unlink()
    except OSError:
        pass


def _history(state_dir: Path, event: str, lock: ReleaseLock, **extra: object) -> None:
    try:
        locked_append_jsonl(
            history_path(state_dir),
            {"at": _iso(datetime.now(UTC)), "event": event, "repo": lock.repo, "reason": lock.reason, "held_by": lock.held_by, **extra},
        )
    except OSError:
        pass


def _remote(repo: str, forge: LockForge) -> ReleaseLock | None:
    try:
        raw = forge.get_variable(repo, LOCK_VARIABLE)
    except ForgeWriteFailed:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("repo"):
        return None
    lock = ReleaseLock.from_dict(data)
    lock.marker = f"variable:{LOCK_VARIABLE}"
    return lock


def _merge(local: ReleaseLock | None, remote: ReleaseLock | None) -> ReleaseLock | None:
    if local is None:
        return remote
    if remote is None:
        return local
    known = {item.number for item in local.disarmed}
    local.disarmed.extend(item for item in remote.disarmed if item.number not in known)
    if remote.marker and not local.marker:
        local.marker = remote.marker
    return local


def _local(state_dir: Path, repo: str) -> ReleaseLock | None:
    local = load(state_dir)
    return local if local is not None and local.repo == repo else None


def current(state_dir: Path, repo: str, forge: LockForge, *, now: datetime | None = None) -> ReleaseLock | None:
    """The lock in force for ``repo``: this machine's file, or the repo marker. Expired is not in force."""
    now = now or datetime.now(UTC)
    lock = _merge(_local(state_dir, repo), _remote(repo, forge))
    if lock is None or lock.expired(now):
        return None
    return lock


# -- messages ------------------------------------------------------------------


def comment_for(lock: ReleaseLock, event: str, *, why: str = "", head_sha: str = "", error: str = "") -> str:
    if event == "disarmed":
        return (
            f"RIVET disarmed auto-merge on this pull request: a release window is open on {lock.repo} "
            f"({lock.reason}, held by {lock.held_by}). Landing resumes at {lock.lifts_at()} UTC or when the tag "
            "is pushed, whichever comes first, and RIVET will re-arm auto-merge then. "
            "`axi release lock-status` shows the window."
        )
    if event == "rearmed":
        return (
            f"The release window on {lock.repo} closed ({why}). RIVET re-armed auto-merge on this pull request "
            f"at head `{head_sha[:10]}`."
        )
    return (
        f"The release window on {lock.repo} closed ({why}), but auto-merge was not re-armed: {error}. "
        "Arm it again yourself once the head is green."
    )


def announce(lock: ReleaseLock, event: str, *, sender, recipients: tuple[str, ...], why: str = "", rearmed: list[int] | None = None) -> list[dict]:
    """Tell the roster through HERALD. No recipients means no message and no failure."""
    if not recipients:
        return []
    numbers = ", ".join(f"#{item.number}" for item in lock.disarmed)
    if event == "open":
        alert = Alert(
            "release_lock",
            "normal",
            f"Release window open on {lock.repo}: {lock.reason} (held by {lock.held_by}). "
            f"Landing is paused until {lock.lifts_at()} UTC or the tag.",
            f"Disarmed auto-merge on {len(lock.disarmed)} pull request(s): {numbers}." if numbers else "No pull request was armed.",
            "Nothing, unless the window must end early: `axi release unlock`.",
            f"release_lock:{lock.repo}:{lock.taken_at}:open",
        )
    else:
        done = ", ".join(f"#{n}" for n in (rearmed or []))
        alert = Alert(
            "release_lock",
            "normal",
            f"Release window closed on {lock.repo} ({why}). Landing resumes.",
            f"Re-armed auto-merge on {len(rearmed or [])} pull request(s): {done}." if done else "Nothing to re-arm.",
            "Nothing.",
            f"release_lock:{lock.repo}:{lock.taken_at}:closed",
        )
    return send_alerts([alert], tuple(recipients), already_sent=set(), sender=sender)


# -- take, sweep, lift -----------------------------------------------------------


def sweep_armed(state_dir: Path, lock: ReleaseLock, forge: LockForge, *, now: datetime | None = None) -> list[DisarmedPull]:
    """Disarm every armed pull request not yet on the lock, comment, and record it."""
    now = now or datetime.now(UTC)
    known = {item.number for item in lock.disarmed}
    new: list[DisarmedPull] = []
    try:
        armed = forge.armed_pulls(lock.repo)
    except ForgeWriteFailed:
        return []
    for row in armed:
        number = int(row.get("number") or 0)
        if not number or number in known:
            continue
        try:
            forge.disarm(lock.repo, number)
        except ForgeWriteFailed:
            continue
        item = DisarmedPull(
            number=number,
            head_sha=str(row.get("head_sha") or ""),
            merge_method=str(row.get("merge_method") or "merge").lower(),
            disarmed_at=_iso(now),
        )
        lock.disarmed.append(item)
        new.append(item)
        try:
            forge.comment(lock.repo, number, comment_for(lock, "disarmed"))
        except ForgeWriteFailed:
            pass
    if new:
        save(state_dir, lock)
        if lock.marker:
            try:
                forge.set_variable(lock.repo, LOCK_VARIABLE, lock.payload())
            except ForgeWriteFailed:
                pass
    return new


def take(
    state_dir: Path,
    repo: str,
    *,
    reason: str,
    held_by: str,
    forge: LockForge,
    now: datetime | None = None,
    minutes: int = DEFAULT_MINUTES,
    bump_pr: int = 0,
    sender=None,
    recipients: tuple[str, ...] = (),
) -> ReleaseLock:
    """Take the lock for ``repo``. Raises ``LockHeld`` when a release is already in flight."""
    now = now or datetime.now(UTC)
    held = current(state_dir, repo, forge, now=now)
    if held is not None:
        raise LockHeld(held)
    lock = ReleaseLock(
        repo=repo,
        reason=reason,
        held_by=held_by,
        taken_at=_iso(now),
        expires_at=_iso(now + timedelta(minutes=max(1, int(minutes)))),
        bump_pr=int(bump_pr or 0),
    )
    try:
        forge.set_variable(repo, LOCK_VARIABLE, lock.payload())
        lock.marker = f"variable:{LOCK_VARIABLE}"
    except ForgeWriteFailed as exc:
        lock.marker_error = str(exc)
    save(state_dir, lock)
    sweep_armed(state_dir, lock, forge, now=now)
    sent = announce(lock, "open", sender=sender, recipients=recipients)
    lock.announced = any(row.get("sent") for row in sent)
    save(state_dir, lock)
    _history(state_dir, "taken", lock, expires_at=lock.expires_at, disarmed=[item.number for item in lock.disarmed], marker=lock.marker)
    return lock


@dataclass
class LiftResult:
    lock: ReleaseLock | None
    why: str
    rearmed: list[int] = field(default_factory=list)
    not_rearmed: list[tuple[int, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lock": self.lock.to_dict() if self.lock else None,
            "why": self.why,
            "rearmed": list(self.rearmed),
            "not_rearmed": [{"number": n, "why": w} for n, w in self.not_rearmed],
        }


def lift(
    state_dir: Path,
    repo: str,
    *,
    forge: LockForge,
    now: datetime | None = None,
    why: str = "tag pushed",
    sender=None,
    recipients: tuple[str, ...] = (),
) -> LiftResult:
    """Lift the lock: delete the marker, re-arm what was disarmed, comment, and tell the roster.

    Whoever deletes the marker re-arms. A lock already lifted elsewhere is only
    cleared here, so nothing is re-armed twice.
    """
    now = now or datetime.now(UTC)
    remote = _remote(repo, forge)
    lock = _merge(_local(state_dir, repo), remote)
    if lock is None:
        return LiftResult(None, why)
    result = LiftResult(lock, why)
    rearm = True
    if lock.marker or remote is not None:
        rearm = forge.delete_variable(repo, LOCK_VARIABLE)
    if rearm:
        for item in lock.disarmed:
            try:
                forge.arm(repo, item.number, item.head_sha, item.merge_method)
                item.rearmed = "rearmed"
                result.rearmed.append(item.number)
                body = comment_for(lock, "rearmed", why=why, head_sha=item.head_sha)
            except ForgeWriteFailed as exc:
                item.rearmed = f"not re-armed: {exc}"
                result.not_rearmed.append((item.number, str(exc)))
                body = comment_for(lock, "not-rearmed", why=why, error=str(exc))
            try:
                forge.comment(repo, item.number, body)
            except ForgeWriteFailed:
                pass
        announce(lock, "closed", sender=sender, recipients=recipients, why=why, rearmed=result.rearmed)
    clear(state_dir)
    _history(
        state_dir,
        "lifted",
        lock,
        why=why,
        rearmed=result.rearmed,
        not_rearmed=[n for n, _ in result.not_rearmed],
        lifted_elsewhere=not rearm,
    )
    return result


def expire_due(
    state_dir: Path,
    repo: str = "",
    *,
    forge: LockForge,
    now: datetime | None = None,
    sender=None,
    recipients: tuple[str, ...] = (),
) -> LiftResult | None:
    """Lift this machine's lock when its timeout has passed. None when there is nothing due."""
    now = now or datetime.now(UTC)
    local = load(state_dir)
    if local is None or (repo and local.repo != repo) or not local.expired(now):
        return None
    return lift(state_dir, local.repo, forge=forge, now=now, why="timeout", sender=sender, recipients=recipients)


# -- for a person ---------------------------------------------------------------


def render(lock: ReleaseLock | None, *, repo: str, now: datetime | None = None) -> str:
    if lock is None:
        return f"no release lock on {repo}"
    now = now or datetime.now(UTC)
    numbers = ", ".join(f"#{item.number}" for item in lock.disarmed) or "none"
    since = _parse(lock.taken_at).strftime("%H:%MZ")
    where = lock.marker or ("local file only" + (f" ({lock.marker_error})" if lock.marker_error else ""))
    return (
        f"release lock on {lock.repo}: {lock.reason}, held by {lock.held_by} since {since}; "
        f"lifts at {lock.lifts_at()} ({lock.minutes_left(now)} min) or on tag; "
        f"disarmed: {numbers}; marker: {where}"
    )


def status(state_dir: Path, repo: str, forge: LockForge, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    lock = current(state_dir, repo, forge, now=now)
    return {
        "repo": repo,
        "held": lock is not None,
        "lock": lock.to_dict() if lock else None,
        "minutes_left": lock.minutes_left(now) if lock else 0,
        "line": render(lock, repo=repo, now=now),
    }
