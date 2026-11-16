# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one implementation of what every backup kind needs.  Standard library only.

State snapshots (:mod:`axiom.infra.state_snapshots`), vault backups
(:mod:`axiom.infra.vault_backup`) and the Postgres dump path
(:mod:`axiom.infra.backup`) differ in WHAT they copy and how they hold it.  They
do not differ in how a backup is made trustworthy, and that part lives here
exactly once:

========================  ====================================================
:func:`stage_file` /      write to a sibling temp file, fsync, chmod, then an
:func:`commit_staged` /   atomic rename and a directory fsync: a file is the
:func:`atomic_publish`    old bytes or the new bytes, never a mixture
:class:`HashChainLedger`  append-only, hash-chained event log
:class:`ExternalHead`     the newest ``(seq, head)`` recorded OUTSIDE the backup
                          directory, so a rolled-back or emptied directory is
                          detectable
:func:`manifest_chain`    the chained per-backup manifest (``prev`` + body)
:func:`backup_lock`       one writer at a time
:func:`has_room`,         disk-space preflight; the "same disk as the thing you
:func:`same_device`       are protecting" check
:func:`plan_retention`    grandfather-father-son retention that never leaves
                          zero verified backups
:func:`select_newest_     "restore latest": the newest backup that verifies,
intact`                   with the reason every newer one was passed over
:class:`FaultPoints`      deterministic fault injection (default: off, free)
========================  ====================================================

A backup kind plugs in by satisfying :class:`BackupKind`.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable

try:  # fcntl is POSIX-only; backups still work (unserialised) elsewhere.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# ===========================================================================
# Small helpers
# ===========================================================================

_CHUNK = 1024 * 1024


def canon(obj: Any) -> bytes:
    """Canonical JSON: the bytes every hash in every backup format is taken over."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


# ===========================================================================
# Fault injection
# ===========================================================================


class SimulatedCrash(BaseException):
    """A power cut at a fault point.  A BaseException so no ``except Exception`` can absorb it,
    and the staging cleanup deliberately does NOT run for it (a real crash cleans nothing up)."""


class FaultPoints:
    """Named places in the write path where a drill can force a failure.

    Production never installs a plan, so :meth:`hit` is one attribute test.  A drill does::

        with FAULTS.inject() as plan:
            plan.fail("commit.replace", nth=3)          # the 3rd rename raises OSError
            plan.crash("restore.file", nth=2)           # power cut before the 2nd file
            plan.drop("commit.replace", nth=1)          # the rename silently never happens
            plan.quota(4096)                            # a disk with 4 KiB left

    Points (``ctx`` is passed to ``when=`` predicates): ``stage.write``, ``stage.fsync``,
    ``commit.replace``, ``commit.fsyncdir``, ``ledger.append``, ``restore.file`` (before each
    file of a restore), ``backup.publish`` (before a backup becomes visible), ``lock.acquire``.
    """

    def __init__(self) -> None:
        self._plan: FaultPlan | None = None

    @property
    def active(self) -> bool:
        return self._plan is not None

    def hit(self, point: str, **ctx: Any) -> bool:
        """Return True when the caller should DROP the operation (pretend it happened)."""
        plan = self._plan
        if plan is None:
            return False
        return plan.hit(point, ctx)

    def account(self, nbytes: int, **ctx: Any) -> None:
        """Charge *nbytes* against the injected disk quota, if any."""
        plan = self._plan
        if plan is not None:
            plan.account(nbytes, ctx)

    @contextlib.contextmanager
    def inject(self) -> Iterator[FaultPlan]:
        if self._plan is not None:
            raise RuntimeError("a fault plan is already installed")
        plan = FaultPlan()
        self._plan = plan
        try:
            yield plan
        finally:
            self._plan = None


@dataclass
class _Rule:
    point: str
    action: str  # fail | crash | drop
    nth: int
    exc: BaseException | None
    when: Callable[[Mapping[str, Any]], bool] | None
    repeat: bool = False
    fired: int = 0


class FaultPlan:
    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        self._rules: list[_Rule] = []
        self._quota: int | None = None
        self.written = 0

    def fail(self, point: str, *, nth: int = 1, exc: BaseException | None = None,
             when: Callable[[Mapping[str, Any]], bool] | None = None, repeat: bool = False) -> None:
        """Raise at the *nth* matching hit (and at every later one with ``repeat``: a disk that stays full)."""
        self._rules.append(_Rule(point, "fail", nth, exc, when, repeat))

    def crash(self, point: str, *, nth: int = 1, when: Callable[[Mapping[str, Any]], bool] | None = None) -> None:
        self._rules.append(_Rule(point, "crash", nth, None, when))

    def drop(self, point: str, *, nth: int = 1, when: Callable[[Mapping[str, Any]], bool] | None = None) -> None:
        self._rules.append(_Rule(point, "drop", nth, None, when))

    def quota(self, nbytes: int) -> None:
        self._quota = nbytes

    def hit(self, point: str, ctx: Mapping[str, Any]) -> bool:
        self.hits[point] += 1
        drop = False
        for r in self._rules:
            if r.point != point or (r.when is not None and not r.when(ctx)):
                continue
            r.fired += 1
            if r.fired != r.nth and not (r.repeat and r.fired > r.nth):
                continue
            if r.action == "crash":
                raise SimulatedCrash(point)
            if r.action == "fail":
                raise r.exc or OSError(errno.EIO, f"injected fault at {point}")
            drop = True
        return drop

    def account(self, nbytes: int, ctx: Mapping[str, Any]) -> None:
        self.written += nbytes
        if self._quota is not None and self.written > self._quota:
            raise OSError(errno.ENOSPC, "No space left on device (injected)")


FAULTS = FaultPoints()


# ===========================================================================
# Atomic writes
# ===========================================================================


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def stage_file(directory: Path, writer: Callable[[BinaryIO], None], *, prefix: str = ".tmp-",
               mode: int = 0o600) -> Path:
    """Write *writer*'s bytes to a fresh temp file in *directory*; fsync; chmod.  Returns its path.

    Nothing visible has changed yet.  On failure the temp file is removed, except for a
    :class:`SimulatedCrash` (a real power cut leaves it behind, and so must the drill)."""
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=prefix)
    try:
        with os.fdopen(fd, "wb") as f:
            FAULTS.hit("stage.write", path=tmp, directory=str(directory))
            writer(f)
            f.flush()
            FAULTS.hit("stage.fsync", path=tmp, directory=str(directory))
            os.fsync(f.fileno())
        os.chmod(tmp, mode & 0o7777)
    except BaseException as exc:
        discard_staged(Path(tmp), exc)
        raise
    return Path(tmp)


def discard_staged(tmp: Path, exc: BaseException | None = None) -> None:
    if isinstance(exc, SimulatedCrash):
        return
    with contextlib.suppress(OSError):
        os.unlink(tmp)


def commit_staged(tmp: Path, dest: Path, *, fsync_parent: bool = True) -> None:
    """Atomically make *tmp* the file *dest*."""
    try:
        if FAULTS.hit("commit.replace", path=str(dest), staged=str(tmp)):
            # injected "the write never landed": the rename silently does not happen
            discard_staged(tmp)
            return
        os.replace(tmp, dest)
    except BaseException as exc:
        discard_staged(tmp, exc)
        raise
    if fsync_parent:
        FAULTS.hit("commit.fsyncdir", path=str(dest))
        fsync_dir(dest.parent)


def atomic_publish(dest: Path, data: bytes, *, mode: int = 0o600, tmpdir: Path | None = None) -> None:
    """``dest`` becomes *data* or stays what it was.  Never a half-written file."""
    def write(f: BinaryIO) -> None:
        FAULTS.account(len(data), path=str(dest))
        f.write(data)

    tmp = stage_file(tmpdir or dest.parent, write, mode=mode)
    commit_staged(tmp, dest)


# ===========================================================================
# Locking
# ===========================================================================


@contextlib.contextmanager
def backup_lock(lockfile: Path, *, busy: Callable[[], BaseException], timeout: float = 10.0,
                prepare: Callable[[], None] | None = None) -> Iterator[None]:
    """Exclusive advisory lock on *lockfile*; raise ``busy()`` after *timeout* seconds."""
    if prepare is not None:
        prepare()
    FAULTS.hit("lock.acquire", path=str(lockfile))
    fd = os.open(str(lockfile), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise busy() from None
                    time.sleep(0.05)
        yield
    finally:
        os.close(fd)


# ===========================================================================
# Disk space and devices
# ===========================================================================


def free_bytes(path: Path) -> tuple[int, int]:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    u = shutil.disk_usage(probe)
    return u.free, u.total


def has_room(path: Path, need: int, *, min_free_bytes: int = 0, min_free_fraction: float = 0.0) -> tuple[bool, str]:
    """Would writing *need* bytes under *path* leave at least the floor free?"""
    free, total = free_bytes(path)
    floor = max(min_free_bytes, int(total * min_free_fraction))
    if free - need < floor:
        return False, f"free space {free} B, need up to {need} B, floor is {floor} B"
    return True, ""


def directory_problem(root: Path, noun: str = "backup directory") -> str | None:
    """Why *root* cannot be used at all, in words an operator can act on; ``None`` if it is usable."""
    root = Path(root)
    try:
        if not root.exists() and not root.is_symlink():
            return (f"the {noun} {root} does not exist (renamed, deleted, or on a disk that is not mounted)")
        if not root.is_dir():
            return f"{root} is not a directory (something replaced the {noun} with a file)"
        if not os.access(root, os.R_OK | os.X_OK):
            return f"the {noun} {root} is not readable by this user (permission denied)"
    except OSError as exc:
        return f"the {noun} {root} cannot be examined ({exc.strerror or exc.errno})"
    return None


def same_device(a: Path, b: Path) -> bool:
    def dev(p: Path) -> int | None:
        p = Path(p)
        while not p.exists() and p != p.parent:
            p = p.parent
        try:
            return os.stat(p).st_dev
        except OSError:
            return None

    da, db = dev(a), dev(b)
    return da is not None and da == db


def same_device_warning(root: Path, protected: Path, advice: str, *, what: str = "backup dir",
                        of: str = "") -> str | None:
    """The warning for a backup directory on the same disk as what it protects, or ``None``."""
    if same_device(root, protected):
        return f"{what} {root} is on the same device as {of or protected}: a disk loss takes both. {advice}"
    return None


# ===========================================================================
# Hash-chained ledger and its external head
# ===========================================================================


@dataclass
class LedgerCheck:
    ok: bool
    entries: int
    head: str
    problems: list[str] = field(default_factory=list)
    #: 1-based number of the first entry that fails (entries before it are still vouched for)
    first_bad: int | None = None


class HashChainLedger:
    """``{seq, ts, event, id, detail, prev, hash}`` lines; ``hash`` covers everything else.

    Both on-disk formats that predate this class are exactly this format."""

    def __init__(self, path: Path, *, ensure: Callable[[], None] | None = None) -> None:
        self.path = Path(path)
        self._ensure = ensure

    def entries(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            text = self.path.read_text("utf-8")
        except OSError:
            return out
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"_corrupt": line[:80]})
        return out

    def head(self) -> str:
        entries = self.entries()
        return entries[-1].get("hash", "") if entries and "_corrupt" not in entries[-1] else ""

    def append(self, event: str, bid: str | None, detail: dict[str, Any]) -> dict[str, Any]:
        entries = self.entries()
        prev = entries[-1].get("hash", "") if entries and "_corrupt" not in entries[-1] else ""
        entry: dict[str, Any] = {"seq": len(entries) + 1, "ts": iso(utcnow()), "event": event,
                                 "id": bid, "detail": detail, "prev": prev}
        entry["hash"] = sha256_bytes(canon(entry))
        if self._ensure is not None:
            self._ensure()
        FAULTS.hit("ledger.append", path=str(self.path), event=event)
        fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            FAULTS.account(len(canon(entry)) + 1, path=str(self.path))
            os.write(fd, canon(entry) + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        return entry

    def verify(self) -> LedgerCheck:
        entries = self.entries()
        problems: list[str] = []
        prev = ""
        first_bad: int | None = None
        for i, e in enumerate(entries, 1):
            if "_corrupt" in e:
                problems.append(f"ledger line {i} is not valid JSON")
                first_bad = i
                break
            body = {k: v for k, v in e.items() if k != "hash"}
            if e.get("seq") != i:
                problems.append(f"ledger entry {i}: sequence is {e.get('seq')}")
            if e.get("prev") != prev:
                problems.append(f"ledger entry {i}: chain does not link to the previous entry")
            if e.get("hash") != sha256_bytes(canon(body)):
                problems.append(f"ledger entry {i}: hash does not match its content")
            if problems:
                first_bad = i
                break
            prev = e["hash"]
        return LedgerCheck(ok=not problems, entries=len(entries), head=prev, problems=problems,
                           first_bad=first_bad)


class ExternalHead:
    """The newest ``(seq, head)`` of each ledger, kept OUTSIDE the backup directory.

    Restoring an old copy of the directory, or deleting its ledger, leaves the ledger shorter
    than (or different from) what this host last wrote.  Keyed by the resolved backup root so
    one file serves a primary and a second copy."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _key(root: Path) -> str:
        return str(Path(root).resolve())

    def record(self, root: Path, ledger: HashChainLedger) -> None:
        entries = ledger.entries()
        if not entries or "_corrupt" in entries[-1]:
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        roots = data.setdefault("roots", {})
        known = roots.get(self._key(root))
        if isinstance(known, dict):
            kseq = int(known.get("seq", 0))
            # Only ever EXTEND what was recorded.  A ledger shorter than the record, or whose entry at
            # the recorded position is a different entry, is rollback/tamper evidence: keep it, so
            # appending to a rolled-back ledger cannot launder the rollback into the record.
            if len(entries) < kseq or entries[kseq - 1].get("hash") != known.get("head"):
                return
        roots[self._key(root)] = {"seq": entries[-1]["seq"], "head": entries[-1]["hash"]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_publish(self.path, json.dumps(data, sort_keys=True).encode())

    def check(self, root: Path, ledger: HashChainLedger) -> list[str]:
        """Rollback / tamper evidence.  An absent record means "never recorded", not a problem."""
        try:
            raw = self.path.read_text("utf-8")
        except OSError:
            return []
        try:
            rec = json.loads(raw)["roots"][self._key(root)]
            seq = int(rec["seq"])
        except KeyError:
            return []
        except (ValueError, TypeError):
            return [f"head record {self.path.name} is unreadable, so rollback cannot be ruled out"]
        entries = ledger.entries()
        accept = (f" If the directory was replaced on purpose, accept it by removing its entry from {self.path}.")
        if not entries and not ledger.path.exists():
            return [f"LEDGER MISSING: this host last wrote ledger entry {seq}, but the backup directory has "
                    "no ledger; it may have been replaced or emptied." + accept]
        if len(entries) < seq:
            return [f"ROLLBACK SUSPECTED: this host last wrote ledger entry {seq}, but the backup "
                    f"directory only has {len(entries)}; it may have been replaced by an older copy." + accept]
        if entries[seq - 1].get("hash") != rec.get("head"):
            return [f"TAMPER SUSPECTED: ledger entry {seq} differs from what this host recorded." + accept]
        return []


# ===========================================================================
# Chained manifests
# ===========================================================================


def manifest_chain(prev: str, body: Mapping[str, Any]) -> tuple[str, str]:
    """``(body_sha256, chain_hash)``; ``chain_hash = sha256(prev + "\\n" + body_sha256)``."""
    body_sha = sha256_bytes(canon(body))
    return body_sha, sha256_bytes(f"{prev}\n{body_sha}".encode())


def build_manifest(prev: str, body: Mapping[str, Any]) -> dict[str, Any]:
    body_sha, chain = manifest_chain(prev, body)
    return {"body": dict(body), "prev": prev, "body_sha256": body_sha, "chain_hash": chain}


def check_manifest(m: Mapping[str, Any]) -> list[str]:
    body = m.get("body")
    if not isinstance(body, dict):
        return ["manifest has no body"]
    body_sha, chain = manifest_chain(m.get("prev", ""), body)
    problems = []
    if body_sha != m.get("body_sha256"):
        problems.append("manifest body does not match its recorded hash (edited or bit-rotted)")
    if chain != m.get("chain_hash"):
        problems.append("chain hash does not match prev + body (chain broken)")
    return problems


# ===========================================================================
# Retention
# ===========================================================================

_FUTURE_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True)
class RetentionPolicy:
    """Grandfather-father-son retention with hard caps.  Defaults explained.

    * ``keep_recent_hours`` 24 -- keep EVERYTHING from the last day: the
      emergency you are most likely to have is "undo what I just did".
    * ``keep_daily`` 14 / ``keep_weekly`` 8 / ``keep_monthly`` 6 -- thinning
      thereafter (restic semantics: the newest backup of each of the N most
      recent days / ISO weeks / months that have one).
    * ``keep_last`` 24 -- always the newest 24 regardless of age.
    * ``protect_newest`` 3 -- never prunable, whatever the other numbers say.
    * ``max_count`` 100 / ``max_bytes`` 2 GiB -- hard caps; beyond them the
      coarsest, oldest history goes first (monthly, weekly, daily, recent).
    * ``min_free_bytes`` 1 GiB / ``min_free_fraction`` 5% -- below this, prune
      harder; if still short, refuse to write.
    """

    keep_last: int = 24
    keep_recent_hours: int = 24
    keep_daily: int = 14
    keep_weekly: int = 8
    keep_monthly: int = 6
    protect_newest: int = 3
    max_count: int = 100
    max_bytes: int = 2 * 1024**3
    min_free_bytes: int = 1024**3
    min_free_fraction: float = 0.05

    def normalized(self) -> RetentionPolicy:
        """Clamp nonsense (negatives, zero protect) rather than trusting config."""
        return replace(
            self,
            keep_last=max(0, int(self.keep_last)),
            keep_recent_hours=max(0, int(self.keep_recent_hours)),
            keep_daily=max(0, int(self.keep_daily)),
            keep_weekly=max(0, int(self.keep_weekly)),
            keep_monthly=max(0, int(self.keep_monthly)),
            protect_newest=max(1, int(self.protect_newest)),
            max_count=max(0, int(self.max_count)),
            max_bytes=max(0, int(self.max_bytes)),
        )

    def harder(self) -> RetentionPolicy:
        """The disk-pressure policy: a quarter of the dense tiers, half the rest."""
        p = self.normalized()
        return replace(
            p,
            keep_last=max(p.protect_newest, p.keep_last // 4),
            keep_recent_hours=p.keep_recent_hours // 4,
            keep_daily=p.keep_daily // 2,
            keep_weekly=p.keep_weekly // 2,
            keep_monthly=p.keep_monthly // 2,
            max_count=(p.max_count // 2) if p.max_count else 0,
            max_bytes=(p.max_bytes // 2) if p.max_bytes else 0,
        )


@dataclass
class BackupInfo:
    """What the retention planner needs to know about one backup."""

    id: str
    created_at: datetime
    size_bytes: int = 0
    verified: bool | None = None  # True ok, False corrupt, None unknown
    pinned: bool = False
    blobs: Mapping[str, int] | None = None  # sha -> stored bytes, for exact dedup accounting


@dataclass(frozen=True)
class Decision:
    id: str
    action: str  # "keep" | "delete"
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RetentionPlan:
    decisions: tuple[Decision, ...]
    warnings: tuple[str, ...] = ()
    kept_bytes: int = 0
    over_budget: bool = False

    @property
    def keep(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == "keep"]

    @property
    def delete(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == "delete"]

    def reasons_for(self, sid: str) -> tuple[str, ...]:
        for d in self.decisions:
            if d.id == sid:
                return d.reasons
        return ()


_TIER_RANK = {"recent": 0, "last": 0, "daily": 1, "weekly": 2, "monthly": 3}


def _bytes_of(snaps: Sequence[BackupInfo]) -> int:
    union: dict[str, int] = {}
    plain = 0
    for s in snaps:
        if s.blobs is not None:
            union.update(s.blobs)
        else:
            plain += s.size_bytes
    return plain + sum(union.values())


def plan_retention(
    snapshots: Sequence[BackupInfo],
    policy: RetentionPolicy,
    now: datetime,
    *,
    pressure: bool = False,
) -> RetentionPlan:
    """Decide what to keep and why.  Pure: no clock, no disk, no side effects.

    Invariants (each has a test that fails if you break it):
      1. the newest ``protect_newest`` backups are kept;
      2. a pinned backup is kept;
      3. the newest verified backup is kept, and if no backup is verified
         nothing at all is deleted;
      4. otherwise the plan is the union of the GFS tiers, then trimmed to the
         count / byte caps coarsest-and-oldest first.
    """
    p = policy.harder() if pressure else policy.normalized()
    warnings: list[str] = []
    if not snapshots:
        return RetentionPlan(decisions=())

    # Clock skew: a backup dated in the future is clamped to ``now`` for
    # ordering and bucketing (it must not displace real history), and flagged.
    def eff(s: BackupInfo) -> datetime:
        return min(s.created_at, now) if s.created_at > now + _FUTURE_TOLERANCE else s.created_at

    for s in snapshots:
        if s.created_at > now + _FUTURE_TOLERANCE:
            warnings.append(f"{s.id}: dated in the future ({s.created_at.isoformat()}); treated as 'now'")

    # newest first; ties broken by id so the plan is deterministic
    ordered = sorted(snapshots, key=lambda s: (eff(s), s.id), reverse=True)
    reasons: dict[str, list[str]] = {s.id: [] for s in ordered}

    verified = [s for s in ordered if s.verified is True]
    if not verified:
        warnings.append("no snapshot is verified: nothing will be pruned")
        return RetentionPlan(
            decisions=tuple(Decision(s.id, "keep", ("no-verified-snapshot-exists",)) for s in ordered),
            warnings=tuple(warnings),
            kept_bytes=_bytes_of(ordered),
        )

    for s in ordered[: p.protect_newest]:
        reasons[s.id].append(f"protected-newest-{p.protect_newest}")
    for s in ordered:
        if s.pinned:
            reasons[s.id].append("pinned")
    reasons[verified[0].id].append("newest-verified")

    # Corrupt backups occupy no tier slot: they are kept only by the guards above.
    healthy = [s for s in ordered if s.verified is not False]

    recent_cut = now - timedelta(hours=p.keep_recent_hours)
    for s in healthy:
        if p.keep_recent_hours and eff(s) >= recent_cut:
            reasons[s.id].append("recent")
    for s in healthy[: p.keep_last]:
        reasons[s.id].append("keep_last")

    def bucket(label: str, keyf: Callable[[datetime], str], n: int) -> None:
        seen: list[str] = []
        for s in healthy:  # newest first, so the first per key is the newest
            k = keyf(eff(s))
            if k in seen:
                continue
            if len(seen) >= n:
                break
            seen.append(k)
            reasons[s.id].append(f"{label}:{k}")

    if p.keep_daily:
        bucket("daily", lambda d: d.astimezone(UTC).strftime("%Y-%m-%d"), p.keep_daily)
    if p.keep_weekly:
        bucket("weekly", lambda d: f"{d.astimezone(UTC).isocalendar()[0]}-W{d.astimezone(UTC).isocalendar()[1]:02d}", p.keep_weekly)
    if p.keep_monthly:
        bucket("monthly", lambda d: d.astimezone(UTC).strftime("%Y-%m"), p.keep_monthly)

    kept = [s for s in ordered if reasons[s.id]]
    by_id = {s.id: s for s in ordered}

    def hard(s: BackupInfo) -> bool:
        return any(
            r.startswith(("protected-newest", "pinned", "newest-verified")) for r in reasons[s.id]
        )

    def rank(s: BackupInfo) -> int:
        ranks = [_TIER_RANK.get(r.split(":", 1)[0].replace("keep_", ""), 0) for r in reasons[s.id]]
        return min(ranks) if ranks else 0

    over = False

    def over_caps(ks: Sequence[BackupInfo]) -> bool:
        return bool((p.max_count and len(ks) > p.max_count) or (p.max_bytes and _bytes_of(ks) > p.max_bytes))

    evictable = sorted(
        (s for s in kept if not hard(s)), key=lambda s: (-rank(s), eff(s), s.id)
    )
    while over_caps(kept) and evictable:
        victim = evictable.pop(0)
        kept = [s for s in kept if s.id != victim.id]
        reasons[victim.id] = []
        reasons[victim.id].append("evicted-over-cap")
    if over_caps(kept):
        over = True
        warnings.append(
            "still over the count/byte cap after evicting everything not protected "
            "(protected, pinned and newest-verified snapshots are never pruned)"
        )

    kept_ids = {s.id for s in kept}
    decisions = []
    for s in ordered:
        if s.id in kept_ids:
            decisions.append(Decision(s.id, "keep", tuple(reasons[s.id])))
        else:
            why = tuple(reasons[s.id]) or (
                ("corrupt-and-not-protected",) if s.verified is False else ("outside-retention-window",)
            )
            decisions.append(Decision(s.id, "delete", why))
    # Safety net: pruning must never leave zero verified backups.
    if not any(d.action == "keep" and by_id[d.id].verified is True for d in decisions):  # pragma: no cover
        decisions = [Decision(d.id, "keep", ("safety-net",)) for d in decisions]
    return RetentionPlan(
        decisions=tuple(decisions), warnings=tuple(warnings),
        kept_bytes=_bytes_of([by_id[d.id] for d in decisions if d.action == "keep"]),
        over_budget=over,
    )


# ===========================================================================
# What a backup kind is, and "restore latest" over any of them
# ===========================================================================


@runtime_checkable
class BackupKind(Protocol):
    """The minimum a backup kind offers so lifecycle code can treat all kinds alike."""

    #: "state snapshot" / "vault backup" / "database dump": for messages.
    noun: str

    def candidates(self) -> list[str]:
        """Backup ids, newest first."""

    def check(self, bid: str) -> list[str]:
        """Why *bid* cannot be trusted; ``[]`` means intact."""


@dataclass
class Rejection:
    id: str
    reasons: list[str]


@dataclass
class Selection:
    """The outcome of choosing a backup for ``latest``."""

    chosen: str | None
    rejected: list[Rejection] = field(default_factory=list)

    @property
    def fell_back(self) -> bool:
        return self.chosen is not None and bool(self.rejected)

    def explain(self, noun: str) -> list[str]:
        """Human lines: which newer backups were passed over and why."""
        lines = []
        for r in self.rejected:
            lines.append(f"skipped {noun} {r.id}: {r.reasons[0] if r.reasons else 'failed verification'}"
                         + (f" (+{len(r.reasons) - 1} more)" if len(r.reasons) > 1 else ""))
        if self.fell_back:
            lines.append(f"using the newest {noun} that verifies: {self.chosen}")
        return lines


def select_newest_intact(kind: BackupKind) -> Selection:
    """Walk *kind*'s backups newest-first; the first that verifies is chosen.  Never a bad one."""
    sel = Selection(chosen=None)
    for bid in kind.candidates():
        problems = kind.check(bid)
        if not problems:
            sel.chosen = bid
            return sel
        sel.rejected.append(Rejection(bid, problems))
    return sel
