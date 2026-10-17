# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Harness-state snapshots: take them, trust them, restore from them, age them out.

What this covers, and what it does not
--------------------------------------

COVERED: the file-based harness state under the user state dir (identity,
settings, consents, approval queue, sessions, user commands, memory ledger
sidecars, audit chains, schedule state ...).  Each file is tagged ``critical``,
``useful`` or ``regenerable`` by a declared rule list (:data:`DEFAULT_RULES`);
caches and large regenerable data are excluded and the manifest says what was
left out and why.

NOT COVERED, and said so in every output:

* **Databases.**  Postgres-backed stores are not dumped here.  The manifest
  carries a pointer to the supported dump path (``data backup``, built on
  :mod:`axiom.infra.backup`) and ``--with-db`` runs that path.
* **Secret VALUES.**  Credential value stores, keychain and vault values are
  never copied; only their names are recorded (``secret_metadata``).  A restore
  therefore brings back every record that *refers* to a credential, and the
  credential itself is re-entered or re-issued.
* Anything under ``snapshots/`` (a snapshot never contains snapshots).

Storage and integrity
---------------------

``<root>/blobs/<aa>/<sha256>``     content-addressed, gzip-stored, deduplicated
``<root>/snaps/<id>/manifest.json`` one directory per snapshot; it exists or it
                                    does not (built in ``tmp/`` and renamed)
``<root>/ledger.jsonl``             hash-chained, append-only event log:
                                    snapshot / prune / restore / pin decisions
``<root>/snaps/<id>/PINNED``        pin marker (outside the hash chain on purpose)

Each manifest carries ``prev`` (the previous snapshot's ``chain_hash``) and its
own ``chain_hash = sha256(prev + body_sha256)``.  ``verify`` re-hashes every
blob, recomputes the chain, and cross-checks the ledger, which also records
every prune, so a *removed* snapshot is distinguishable from a *pruned* one.
A hash chain is used rather than the attest machinery because the emergency
restore script (``scripts/axi-state-restore.py``) must verify with the Python
standard library alone.  A chain cannot stop someone who rewrites the whole
store; record ``ledger_head`` (printed by ``verify``) somewhere else if that
matters to you.

Everything in the snapshot root is owner-only (0700 dirs, 0600 files): the
files copied in may be secret-bearing (identity private keys, node keys).

Safety rules (property-tested)
------------------------------

* Restore never overwrites in place.  Each file is staged, the current file is
  preserved as ``<name>.pre-restore-<ts>``, the staged file replaces it with an
  atomic rename, and the result is re-hashed.  Nothing is ever deleted.
* A restore first takes a pre-restore snapshot and refuses if that fails.
* A snapshot that fails ``verify`` is not restored (without ``force``), and
  paths in a manifest are confined to the state dir (a tampered manifest
  cannot write elsewhere).
* Retention never prunes: the newest N, a pinned snapshot, the newest verified
  snapshot, or anything at all if it would leave no verified snapshot.
"""

from __future__ import annotations

import contextlib
import errno
import gzip
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import stat
import tempfile
import time
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra import backup_core as bc

# Retention, the ledger, atomic writes, locks and disk checks are shared with every other
# backup kind (see backup_core).  These names are this module's public surface.
from axiom.infra.backup_core import (  # noqa: F401  (re-exported)
    Decision,
    RetentionPlan,
    RetentionPolicy,
    plan_retention,
)

try:  # fcntl is POSIX-only; running-service detection degrades without it.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SnapshotInfo = bc.BackupInfo
SCHEMA_VERSION = 1
TIERS = ("critical", "useful", "regenerable")
#: Critical files are held in memory while stored; above this they are excluded (and say so).
MAX_CRITICAL_FILE_BYTES = 1024**3

#: The address a developer's own database lives at.  ``--with-db`` refuses it
#: without an explicit ``--yes-this-database``.
DEFAULT_DEV_DB_ADDRESSES = frozenset(
    {("localhost", 5432), ("127.0.0.1", 5432), ("::1", 5432), ("", 5432)}
)


class SnapshotError(RuntimeError):
    """A snapshot operation could not be completed safely."""


class DiskPressureError(SnapshotError):
    """There is not enough free space to snapshot without filling the disk."""


class SnapshotBusy(SnapshotError):
    """Another snapshot / prune / restore holds the snapshot-root lock."""


def _write_failed(exc: OSError, what: str) -> SnapshotError:
    """A failed write as an operator-readable error; the existing snapshots are untouched."""
    if exc.errno == errno.ENOSPC:
        return DiskPressureError(
            f"the disk filled while writing {what}; nothing was published and the existing snapshots are "
            "untouched. Free space or point snapshots at another disk with --to <dir>."
        )
    return SnapshotError(
        f"writing {what} failed ({exc.strerror or exc}); nothing was published and the existing "
        "snapshots are untouched"
    )


# ===========================================================================
# Policy
# ===========================================================================


@dataclass(frozen=True)
class SnapshotPolicy:
    """Everything configurable under ``state.snapshots.*``."""

    enabled: bool = True
    interval_hours: float = 6.0
    auto_prune: bool = True
    max_file_bytes: int = 64 * 1024 * 1024
    include_regenerable: bool = False
    extra_exclude: tuple[str, ...] = ()
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)


_SETTING_PREFIX = "state.snapshots."


def policy_from_settings(settings: Mapping[str, Any] | None = None) -> SnapshotPolicy:
    """Build a policy from flat ``state.snapshots.*`` keys; unknown keys ignored."""
    s = dict(settings or {})
    base = SnapshotPolicy()
    r = base.retention

    def _get(name: str, default: Any, cast: Callable[[Any], Any]) -> Any:
        raw = s.get(_SETTING_PREFIX + name, default)
        try:
            return cast(raw)
        except (TypeError, ValueError):
            log.warning("ignoring invalid setting %s%s=%r", _SETTING_PREFIX, name, raw)
            return default

    def _bool(v: Any) -> bool:
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "on"}
        return bool(v)

    extra = _get("exclude", (), lambda v: tuple(str(x) for x in (v or ())))
    retention = RetentionPolicy(
        keep_last=_get("keep_last", r.keep_last, int),
        keep_recent_hours=_get("keep_recent_hours", r.keep_recent_hours, int),
        keep_daily=_get("keep_daily", r.keep_daily, int),
        keep_weekly=_get("keep_weekly", r.keep_weekly, int),
        keep_monthly=_get("keep_monthly", r.keep_monthly, int),
        protect_newest=_get("protect_newest", r.protect_newest, int),
        max_count=_get("max_count", r.max_count, int),
        max_bytes=_get("max_bytes", r.max_bytes, int),
        min_free_bytes=_get("min_free_bytes", r.min_free_bytes, int),
        min_free_fraction=_get("min_free_fraction", r.min_free_fraction, float),
    ).normalized()
    return SnapshotPolicy(
        enabled=_get("enabled", base.enabled, _bool),
        interval_hours=_get("interval_hours", base.interval_hours, float),
        auto_prune=_get("auto_prune", base.auto_prune, _bool),
        max_file_bytes=_get("max_file_bytes", base.max_file_bytes, int),
        include_regenerable=_get("include_regenerable", base.include_regenerable, _bool),
        extra_exclude=extra,
        retention=retention,
    )


def load_policy(state_dir: Path | None = None) -> SnapshotPolicy:
    """Read ``[state.snapshots]`` from ``<state>/settings.toml`` (global scope)."""
    sd = Path(state_dir) if state_dir else _default_state_dir()
    flat: dict[str, Any] = {}
    try:
        import tomllib

        data = tomllib.loads((sd / "settings.toml").read_text(encoding="utf-8"))
        table = data.get("state", {}).get("snapshots", {})
        if isinstance(table, dict):
            flat = {_SETTING_PREFIX + k: v for k, v in table.items()}
        # Tolerate the dotted-key spelling too.
        for k, v in data.items():
            if isinstance(k, str) and k.startswith(_SETTING_PREFIX):
                flat[k] = v
    except (OSError, ValueError):
        pass
    return policy_from_settings(flat)


# ===========================================================================
# Paths
# ===========================================================================


def _default_state_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def default_snapshot_root(state_dir: Path | None = None) -> Path:
    """``<state>/snapshots`` -- excluded from its own snapshots by rule."""
    return (Path(state_dir) if state_dir else _default_state_dir()) / "snapshots"


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def _axiom_version() -> str:
    try:
        from importlib.metadata import version

        return version("axiom-os-lm")
    except Exception:  # noqa: BLE001
        return "unknown"


# ===========================================================================
# Rules: what is in a snapshot
# ===========================================================================


def _compile_glob(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(pattern: str, relpath: str) -> bool:
    """``**`` crosses directories, ``*`` and ``?`` do not.  Paths are posix, state-relative."""
    return _compile_glob(pattern).match(relpath) is not None


@dataclass(frozen=True)
class SnapshotRule:
    pattern: str
    action: str  # "include" | "exclude"
    tier: str
    reason: str

    def matches(self, relpath: str) -> bool:
        return glob_match(self.pattern, relpath)


def _x(pattern: str, tier: str, reason: str) -> SnapshotRule:
    return SnapshotRule(pattern, "exclude", tier, reason)


def _i(pattern: str, tier: str, reason: str) -> SnapshotRule:
    return SnapshotRule(pattern, "include", tier, reason)


#: First match wins.  Order matters: secrets and the snapshot store's own
#: directory are excluded before anything can include them.
DEFAULT_RULES: tuple[SnapshotRule, ...] = (
    # -- never --------------------------------------------------------------
    _x("snapshots/**", "regenerable", "the snapshot store never contains itself"),
    _x("vault-backups/**", "critical", "encrypted vault backups are their own store (see spec-vault-backup.md)"),
    _x("secrets/snapshot-head.json", "critical", "rollback evidence kept OUTSIDE the store; restoring it would erase the evidence"),
    _x("secrets/vault-backup-*.json", "critical", "vault-backup key-custody and bookkeeping records live with the vault backup"),
    _x("credentials/**", "critical", "credential values are never copied into a snapshot"),
    _x("secrets/foreign-values.json", "critical", "credential values are never copied into a snapshot"),
    _x("**/*.secret", "critical", "secret material is never copied into a snapshot"),
    _x("**/fleet-token*", "critical", "bearer tokens are never copied into a snapshot"),
    _x("**/*.pre-restore-*", "regenerable", "files set aside by a restore; remove once the restore is confirmed"),
    _x("**/.*.restore-*", "regenerable", "staged file of an interrupted restore (never state)"),
    _x("**/.tmp-*", "regenerable", "staged file of an interrupted atomic write (never state)"),
    # -- volatile ------------------------------------------------------------
    _x("**/*.pid", "regenerable", "process id file (volatile)"),
    _x("**/*.sock", "regenerable", "socket (volatile)"),
    _x("**/*.lock", "regenerable", "advisory lock file (volatile)"),
    _x("locks/**", "regenerable", "advisory locks (volatile)"),
    _x("**/*.tmp", "regenerable", "temporary file"),
    _x("**/*-shm", "regenerable", "sqlite shared-memory sidecar (the .db is captured consistently)"),
    _x("**/*-wal", "regenerable", "sqlite write-ahead sidecar (the .db is captured consistently)"),
    # -- caches / large regenerable data -------------------------------------
    _x("service-venv/**", "regenerable", "python environment (reinstallable)"),
    _x("**/__pycache__/**", "regenerable", "bytecode cache"),
    _x("**/.venv/**", "regenerable", "python environment (reinstallable)"),
    _x("**/node_modules/**", "regenerable", "dependency cache (reinstallable)"),
    _x("**/.cache/**", "regenerable", "cache"),
    _x("**/cache/**", "regenerable", "cache"),
    _x("downloads/**", "regenerable", "downloaded artifacts (re-downloadable)"),
    _x("model-storage/**", "regenerable", "model weights (re-downloadable)"),
    _x("llamafile/**", "regenerable", "model binaries (re-downloadable)"),
    _x("packs/**", "regenerable", "installed knowledge packs (reinstallable)"),
    _x("traces/**", "regenerable", "trace output"),
    _x("logs/**", "regenerable", "log output"),
    _x("**/*.log", "regenerable", "log output"),
    _x("**/*.log.*", "regenerable", "rotated log output"),
    _x("backups/**", "regenerable", "database dumps are their own backup store (see the db section)"),
    _x("memory/recall.db", "regenerable", "derived recall projection, rebuilt from the memory ledger"),
    _x("agents/.background-service/**", "regenerable", "scheduler tick log"),
    _x("telemetry/**", "regenerable", "usage telemetry"),
    # -- critical -------------------------------------------------------------
    _i("identity/**", "critical", "node/user identity and keys"),
    _i("settings.toml", "critical", "user settings"),
    _i("config/**", "critical", "configuration"),
    _i("**/*consent*.json", "critical", "consents"),
    _i("gate/**", "critical", "access gate state"),
    _i("policy/**", "critical", "policy directives"),
    _i("memory/**", "critical", "memory ledger and sidecars"),
    _i("attest/**", "critical", "signed records"),
    _i("audit/**", "critical", "audit chains"),
    _i("orchestrator/**", "critical", "approval queue"),
    _i("agents/commands/**", "critical", "user commands"),
    _i("commands/**", "critical", "user commands"),
    _i("nodes.yaml", "critical", "node registry"),
    _i("profile.json", "critical", "user profile"),
    _i("directory/**", "critical", "directory state"),
    _i("federation/**", "critical", "federation state"),
    _i("secrets/foreign-credentials.json", "critical", "credential METADATA index (names, expiry; no values)"),
    # -- everything else that is a file is useful -----------------------------
    _i("sessions/**", "useful", "chat session history and checkpoints"),
    _i("**", "useful", "harness state (default: protect new state rather than miss it)"),
)


#: Files that grow on their own whenever the harness runs.  They ARE captured,
#: but a change confined to them does not by itself justify a new snapshot --
#: otherwise "skip if nothing changed" could never fire on a live harness,
#: including because the scheduled snapshot's own bookkeeping appends to them.
CHURN_GLOBS: tuple[str, ...] = (
    "audit/**",
    "schedule/audit/**",
    "agents/*/heartbeat.jsonl",
    "agents/*/sweep.jsonl",
    "**/*.corrupt-*",
)


def is_churn(relpath: str) -> bool:
    return any(glob_match(g, relpath) for g in CHURN_GLOBS)


def classify(
    relpath: str, rules: Sequence[SnapshotRule] = DEFAULT_RULES
) -> SnapshotRule:
    for rule in rules:
        if rule.matches(relpath):
            return rule
    return _i("**", "useful", "default")


def rules_with_extra_excludes(
    extra: Sequence[str], rules: Sequence[SnapshotRule] = DEFAULT_RULES
) -> tuple[SnapshotRule, ...]:
    """User excludes sit FIRST: a user who excludes something means it."""
    user = tuple(_x(p, "regenerable", "excluded by state.snapshots.exclude") for p in extra)
    return user + tuple(rules)


# ===========================================================================
# Small IO helpers
# ===========================================================================


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


@contextlib.contextmanager
def _stable_copy(path: Path) -> Iterator[Path]:
    """Yield a path whose bytes are a consistent image of *path*.

    A live SQLite database copied byte-by-byte can be torn mid-write; the
    backup API gives a transactionally consistent image.  Everything else is
    read in place.
    """
    if path.suffix in {".db", ".sqlite", ".sqlite3"} and _is_sqlite(path):
        tmpdir = tempfile.mkdtemp(prefix="axiom-snap-sqlite-")
        try:
            os.chmod(tmpdir, 0o700)
            dst = Path(tmpdir) / path.name
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                out = sqlite3.connect(str(dst))
                try:
                    src.backup(out)
                finally:
                    out.close()
            finally:
                src.close()
            yield dst
        except sqlite3.Error as exc:
            raise SnapshotError(f"could not take a consistent copy of {path}: {exc}") from exc
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        yield path


# ===========================================================================
# The store
# ===========================================================================


@dataclass
class SnapshotRef:
    """A file as held by one snapshot -- what the chaos-hardening seam returns."""

    snapshot_id: str
    created_at: str
    path: str  # state-relative, posix
    sha256: str
    size: int
    mode: int
    snapshot_verified: bool | None = None


class Store:
    """Filesystem layout + primitives.  All mutation goes through :meth:`locked`.

    ``head_file`` (``<state>/secrets/snapshot-head.json`` via :func:`open_store`) is where the
    newest ledger ``(seq, head)`` is recorded outside the store, so a rolled-back or emptied
    store is detectable (:meth:`head_problems`)."""

    def __init__(self, root: Path, head_file: Path | None = None) -> None:
        self.root = Path(root)
        self.blobs = self.root / "blobs"
        self.snaps = self.root / "snaps"
        self.tmp = self.root / "tmp"
        self.ledger = self.root / "ledger.jsonl"
        self.lockfile = self.root / ".lock"
        self._chain = bc.HashChainLedger(self.ledger, ensure=self.ensure)
        self._head = bc.ExternalHead(head_file) if head_file else None

    def ensure(self) -> None:
        for d in (self.root, self.blobs, self.snaps, self.tmp):
            bc.mkdir_private(d)

    def locked(self, timeout: float = 10.0) -> contextlib.AbstractContextManager[None]:
        return bc.backup_lock(
            self.lockfile, timeout=timeout, prepare=self.ensure,
            busy=lambda: SnapshotBusy(f"another snapshot/prune/restore holds {self.lockfile}"),
        )

    # -- blobs --------------------------------------------------------------
    def blob_path(self, sha: str) -> Path:
        return self.blobs / sha[:2] / sha

    def has_blob(self, sha: str) -> bool:
        return self.blob_path(sha).is_file()

    def put_blob(self, sha: str, data: bytes) -> int:
        """Store *data* under *sha* (caller computed it); returns stored bytes."""
        dest = self.blob_path(sha)
        if dest.is_file():
            return dest.stat().st_size
        bc.mkdir_private(dest.parent)

        def write(raw: Any) -> None:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=3, mtime=0) as gz:
                bc.FAULTS.account(len(data), path=str(dest))
                gz.write(data)

        tmp = bc.stage_file(self.tmp, write, prefix="blob-")
        bc.commit_staged(tmp, dest, fsync_parent=False)
        return dest.stat().st_size

    def read_blob(self, sha: str) -> bytes:
        try:
            with gzip.open(self.blob_path(sha), "rb") as f:
                return f.read()
        except (EOFError, zlib.error) as exc:  # a truncated or rotted stream: the same answer as a missing blob
            raise SnapshotError(f"blob {sha[:12]} is unreadable ({type(exc).__name__})") from exc

    def blob_ok(self, sha: str) -> bool:
        try:
            return bc.sha256_bytes(self.read_blob(sha)) == sha
        except (OSError, SnapshotError):
            return False

    # -- manifests ----------------------------------------------------------
    def snap_dir(self, sid: str) -> Path:
        return self.snaps / sid

    def ids(self) -> list[str]:
        try:
            if not self.snaps.is_dir():
                return []
            return sorted(p.name for p in self.snaps.iterdir() if (p / "manifest.json").is_file())
        except OSError:  # unreadable directory: no snapshots visible, and unavailable_reason says why
            return []

    def read_manifest(self, sid: str) -> dict[str, Any]:
        try:
            return json.loads((self.snap_dir(sid) / "manifest.json").read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise SnapshotError(f"snapshot {sid}: manifest unreadable ({exc})") from exc

    def is_pinned(self, sid: str) -> bool:
        return (self.snap_dir(sid) / "PINNED").exists()

    # -- ledger -------------------------------------------------------------
    def ledger_entries(self) -> list[dict[str, Any]]:
        return self._chain.entries()

    def ledger_head(self) -> str:
        return self._chain.head()

    def ledger_append(self, event: str, sid: str | None, detail: dict[str, Any]) -> dict[str, Any]:
        entry = self._chain.append(event, sid, detail)
        if self._head is not None:
            with contextlib.suppress(OSError):
                self._head.record(self.root, self._chain)
        return entry

    def head_problems(self) -> list[str]:
        """Rollback / emptied-ledger evidence from the record kept outside the store."""
        if self._head is None or bc.directory_problem(self.root):
            return []  # an unusable directory is reported as that, not as a missing ledger
        return self._head.check(self.root, self._chain)



# ===========================================================================
# Scanning the state dir
# ===========================================================================


@dataclass
class ScanEntry:
    path: str
    abs_path: Path
    size: int
    mode: int
    tier: str
    sha256: str = ""


@dataclass
class ScanResult:
    included: list[ScanEntry] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    secret_metadata: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _record_exclusion(
    excl: dict[tuple[str, str, str], dict[str, Any]],
    rule: SnapshotRule,
    size: int,
    why: str | None = None,
    path: str = "",
) -> None:
    key = (rule.pattern, rule.tier, why or rule.reason)
    e = excl.setdefault(
        key,
        {"pattern": rule.pattern, "tier": rule.tier, "reason": why or rule.reason,
         "files": 0, "bytes": 0, "examples": []},
    )
    e["files"] += 1
    e["bytes"] += size
    if path and len(e["examples"]) < 3:
        e["examples"].append(path)


def scan_state(
    state_dir: Path,
    policy: SnapshotPolicy,
    rules: Sequence[SnapshotRule] | None = None,
    *,
    hash_files: bool = True,
) -> ScanResult:
    """Walk *state_dir* once, classify every file, hash the included ones.

    Directories whose probe path matches a ``dir/**`` exclude are pruned from
    the walk (so a 235 MB venv costs nothing); they appear in ``excluded`` with
    ``files: null``.
    """
    rules = tuple(rules) if rules is not None else rules_with_extra_excludes(policy.extra_exclude)
    state_dir = Path(state_dir)
    res = ScanResult()
    excl: dict[tuple[str, str, str], dict[str, Any]] = {}
    skipped_dirs: dict[str, SnapshotRule] = {}

    def walk(dir_abs: Path, dir_rel: str) -> None:
        try:
            entries = sorted(os.scandir(dir_abs), key=lambda e: e.name)
        except OSError as exc:
            res.errors.append(f"{dir_rel or '.'}: unreadable directory ({exc.strerror})")
            return
        for ent in entries:
            rel = f"{dir_rel}/{ent.name}" if dir_rel else ent.name
            try:
                st = ent.stat(follow_symlinks=False)
            except OSError as exc:
                res.errors.append(f"{rel}: {exc.strerror}")
                continue
            if stat.S_ISLNK(st.st_mode):
                _record_exclusion(excl, _x(rel, "regenerable", "symlink"), 0, "symlinks are not followed")
                continue
            if stat.S_ISDIR(st.st_mode):
                rule = classify(rel + "/\0", rules)
                if rule.action == "exclude" and rule.pattern.endswith("/**"):
                    skipped_dirs[rel] = rule
                    continue
                walk(Path(ent.path), rel)
                continue
            if not stat.S_ISREG(st.st_mode):
                _record_exclusion(excl, _x(rel, "regenerable", "not a regular file"), 0, "not a regular file")
                continue
            rule = classify(rel, rules)
            if rule.action == "exclude":
                _record_exclusion(excl, rule, st.st_size, path=rel)
                continue
            if rule.tier == "regenerable" and not policy.include_regenerable:
                _record_exclusion(excl, rule, st.st_size, path=rel)
                continue
            # Critical files get a far higher ceiling: silently dropping the
            # memory ledger because it passed 64 MiB would be the worst
            # possible default (found on a real state dir: an 88 MB ledger db).
            limit = MAX_CRITICAL_FILE_BYTES if rule.tier == "critical" else policy.max_file_bytes
            if st.st_size > limit:
                _record_exclusion(
                    excl, rule, st.st_size,
                    f"larger than the {'critical-file ceiling' if rule.tier == 'critical' else 'state.snapshots.max_file_bytes'}"
                    f" ({limit} bytes)",
                    path=rel,
                )
                continue
            res.included.append(
                ScanEntry(rel, Path(ent.path), st.st_size, stat.S_IMODE(st.st_mode), rule.tier)
            )

    walk(state_dir, "")
    for rel, rule in sorted(skipped_dirs.items()):
        res.excluded.append(
            {"pattern": rule.pattern, "tier": rule.tier, "reason": rule.reason,
             "files": None, "bytes": None, "dir": rel}
        )
        # Names (never values) of the credentials left out.
        if rule.pattern == "credentials/**" and rel == "credentials":
            with contextlib.suppress(OSError):
                res.secret_metadata.extend(
                    f"credentials/{n}" for n in sorted(os.listdir(state_dir / rel))
                )
    res.excluded.extend(sorted(excl.values(), key=lambda e: (e["pattern"], e["reason"])))
    if hash_files:
        for entry in res.included:
            try:
                with _stable_copy(entry.abs_path) as src:
                    entry.sha256 = bc.sha256_file(src)
                    entry.size = src.stat().st_size
            except (OSError, SnapshotError) as exc:
                res.errors.append(f"{entry.path}: {exc}")
                entry.sha256 = ""
        res.included = [e for e in res.included if e.sha256]
    return res


def _inputs_hash(entries: Sequence[ScanEntry | dict[str, Any]]) -> str:
    """Hash of what makes two snapshots "the same": path, content, mode (churn files aside)."""
    lines = []
    for e in entries:
        p, s, m = (e.path, e.sha256, e.mode) if isinstance(e, ScanEntry) else (e["path"], e["sha256"], e["mode"])
        if is_churn(p):
            continue
        lines.append(f"{p}\0{s}\0{m:o}\n")
    return bc.sha256_bytes("".join(sorted(lines)).encode())


# ===========================================================================
# Retention (pure)
# ===========================================================================


# ===========================================================================
# Listing, verifying
# ===========================================================================


@dataclass
class VerifyResult:
    id: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    files_checked: int = 0


def _snapshot_files(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return list(manifest.get("body", {}).get("files", []))


def verify_snapshot(store: Store, sid: str, *, deep: bool = True) -> VerifyResult:
    """Re-derive everything a snapshot claims.  ``deep`` re-hashes every blob."""
    res = VerifyResult(sid, True)
    try:
        m = store.read_manifest(sid)
    except SnapshotError as exc:
        return VerifyResult(sid, False, [str(exc)])
    body = m.get("body")
    if not isinstance(body, dict):
        return VerifyResult(sid, False, ["manifest has no body"])
    res.problems += bc.check_manifest(m)
    if not res.problems:
        ver = body.get("version", SCHEMA_VERSION)
        if not isinstance(ver, int) or ver > SCHEMA_VERSION:
            res.problems.append(
                f"snapshot schema {ver} is newer than this tool understands ({SCHEMA_VERSION}); upgrade "
                "Axiom, or use the emergency script from the newer release"
            )
    seen_paths: set[str] = set()
    for f in _snapshot_files(m):
        res.files_checked += 1
        p, sha = f.get("path", "?"), f.get("sha256", "")
        if p in seen_paths:
            res.problems.append(f"{p}: listed twice")
        seen_paths.add(p)
        if not store.has_blob(sha):
            res.problems.append(f"{p}: blob {sha[:12]} missing")
        elif deep and not store.blob_ok(sha):
            res.problems.append(f"{p}: blob {sha[:12]} does not match its hash (bit-rot or tampering)")
        elif not deep:
            try:
                if store.blob_path(sha).stat().st_size == 0:
                    res.problems.append(f"{p}: blob {sha[:12]} is empty")
            except OSError:
                res.problems.append(f"{p}: blob {sha[:12]} unreadable")
    db = body.get("db", {})
    dump = db.get("dump") if isinstance(db, dict) else None
    if dump and deep:
        dp = store.root / dump.get("relpath", "")
        if not dp.is_file():
            res.problems.append(f"db dump {dump.get('relpath')} missing")
        elif bc.sha256_file(dp) != dump.get("sha256"):
            res.problems.append(f"db dump {dump.get('relpath')} does not match its hash")
    res.ok = not res.problems
    return res


LedgerCheck = bc.LedgerCheck


def verify_ledger(store: Store) -> LedgerCheck:
    return store._chain.verify()


@dataclass
class StoreVerification:
    ok: bool
    results: list[VerifyResult]
    ledger: LedgerCheck
    store_problems: list[str]

    @property
    def verified_ids(self) -> list[str]:
        return [r.id for r in self.results if r.ok]


def verify_store(store: Store, *, deep: bool = True) -> StoreVerification:
    """Every snapshot, the ledger, and the cross-check between them."""
    results = [verify_snapshot(store, sid, deep=deep) for sid in store.ids()]
    ledger = verify_ledger(store)
    problems: list[str] = []
    snap_events: dict[str, str] = {}
    pruned: set[str] = set()
    hashes_ever: set[str] = {""}
    for e in store.ledger_entries():
        if e.get("event") == "snapshot" and e.get("id"):
            snap_events[e["id"]] = e.get("detail", {}).get("chain_hash", "")
            hashes_ever.add(snap_events[e["id"]])
        elif e.get("event") == "prune" and e.get("id"):
            pruned.add(e["id"])
    present = set(store.ids())
    by_id = {r.id: r for r in results}
    for sid in present:
        try:
            m = store.read_manifest(sid)
        except SnapshotError:
            continue  # already reported by verify_snapshot as unreadable
        recorded = snap_events.get(sid)
        if recorded is None:
            by_id[sid].problems.append("snapshot is not in the ledger (added outside the tool?)")
            by_id[sid].ok = False
        elif recorded != m.get("chain_hash"):
            by_id[sid].problems.append("manifest differs from the one the ledger recorded (substituted)")
            by_id[sid].ok = False
        if m.get("prev", "") not in hashes_ever:
            by_id[sid].problems.append("prev link points at a snapshot the ledger never recorded")
            by_id[sid].ok = False
    for sid in snap_events:
        if sid not in present and sid not in pruned:
            problems.append(f"{sid}: recorded as taken, absent, and never pruned (deleted outside the tool?)")
    problems += store.head_problems()
    ok = all(r.ok for r in results) and ledger.ok and not problems
    return StoreVerification(ok, results, ledger, problems)


def list_snapshots(store: Store) -> list[dict[str, Any]]:
    """Light listing (no hashing): one dict per snapshot, newest first."""
    out = []
    for sid in store.ids():
        try:
            m = store.read_manifest(sid)
        except SnapshotError:
            out.append({"id": sid, "created_at": None, "corrupt": True, "pinned": store.is_pinned(sid)})
            continue
        b = m.get("body", {})
        files = b.get("files", [])
        out.append({
            "id": sid,
            "created_at": b.get("created_at"),
            "reason": b.get("reason", ""),
            "trigger": b.get("trigger", ""),
            "files": len(files),
            "bytes": sum(f.get("size", 0) for f in files),
            "pinned": store.is_pinned(sid),
            "critical_files": sum(1 for f in files if f.get("tier") == "critical"),
            "has_db_dump": bool(b.get("db", {}).get("dump")),
        })
    out.sort(key=lambda d: (d.get("created_at") or "", d["id"]), reverse=True)
    return out


def _snapshot_infos(store: Store, verification: StoreVerification | None) -> list[SnapshotInfo]:
    vmap = {r.id: r.ok for r in verification.results} if verification else {}
    infos = []
    for sid in store.ids():
        try:
            m = store.read_manifest(sid)
            created = bc.parse_iso(m["body"]["created_at"])
        except (SnapshotError, KeyError, ValueError):
            created = datetime.fromtimestamp(0, tz=UTC)
            m = {}
        blobs: dict[str, int] = {}
        for f in _snapshot_files(m):
            with contextlib.suppress(OSError):
                blobs[f["sha256"]] = store.blob_path(f["sha256"]).stat().st_size
        dump = m.get("body", {}).get("db", {}).get("dump")
        extra = int(dump.get("size", 0)) if dump else 0
        infos.append(SnapshotInfo(
            id=sid, created_at=created, size_bytes=sum(blobs.values()) + extra,
            verified=vmap.get(sid), pinned=store.is_pinned(sid),
            blobs={**blobs, **({f"db:{sid}": extra} if extra else {})},
        ))
    return infos


# ===========================================================================
# Services (restore refuses while they run)
# ===========================================================================


@dataclass
class RunningService:
    what: str
    detail: str


def _pid_alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def detect_running_services(state_dir: Path, *, max_depth: int = 3) -> list[RunningService]:
    """Best-effort: live ``*.pid`` files and exclusively-held ``*.lock`` files.

    There is no central service registry (services record themselves with pid
    files, advisory locks, or only with the OS service manager).  What this
    cannot see, it cannot see; ``restore`` prints that caveat.
    """
    found: list[RunningService] = []
    state_dir = Path(state_dir)

    def scan(d: Path, depth: int, rel: str) -> None:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for ent in entries:
            r = f"{rel}/{ent.name}" if rel else ent.name
            try:
                if ent.is_symlink():
                    continue
                if ent.is_dir(follow_symlinks=False):
                    rule = classify(r + "/\0")
                    if depth < max_depth and not (
                        rule.action == "exclude" and rule.pattern.endswith("/**")
                        and r.split("/")[0] != "locks"
                    ):
                        scan(Path(ent.path), depth + 1, r)
                    continue
            except OSError:
                continue
            if ent.name.endswith(".pid"):
                try:
                    pid = int(Path(ent.path).read_text().strip().split()[0])
                except (OSError, ValueError, IndexError):
                    continue
                if _pid_alive(pid):
                    found.append(RunningService(r, f"pid {pid} is alive"))
            elif ent.name.endswith(".lock") and fcntl is not None:
                try:
                    fd = os.open(ent.path, os.O_RDONLY)
                except OSError:
                    continue
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError as exc:
                        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                            found.append(RunningService(r, "lock is held by a running process"))
                finally:
                    os.close(fd)

    scan(state_dir, 0, "")
    return found


# ===========================================================================
# Free space
# ===========================================================================


same_device = bc.same_device


def _free_check(root: Path, need: int, policy: RetentionPolicy) -> tuple[bool, str]:
    ok, why = bc.has_room(root, need, min_free_bytes=policy.min_free_bytes,
                          min_free_fraction=policy.min_free_fraction)
    if ok:
        return True, ""
    return False, f"{why} (state.snapshots.min_free_bytes / min_free_fraction)"


# ===========================================================================
# Create
# ===========================================================================


@dataclass
class SnapshotResult:
    ok: bool
    snapshot_id: str | None = None
    skipped: bool = False
    message: str = ""
    files: int = 0
    bytes_logical: int = 0
    bytes_added: int = 0
    warnings: list[str] = field(default_factory=list)
    excluded_summary: list[dict[str, Any]] = field(default_factory=list)
    scan_errors: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    db: dict[str, Any] = field(default_factory=dict)
    root: Path | None = None


def db_pointer(state_dir: Path | None = None) -> dict[str, Any]:
    """What the snapshot does NOT cover, and the supported way to cover it."""
    cli = _brand_cli()
    out: dict[str, Any] = {
        "covered": False,
        "what": "Postgres-backed stores (memory ledger, receipts, schedule, data platform, ...)",
        "how": f"{cli} state snapshot --with-db   (or: {cli} data backup)",
        "restore_with": "psql / pg_restore from the dump; see the runbook in spec-state-snapshots",
    }
    try:
        from axiom.infra.backup import list_backups

        latest = list_backups()[:1]
        if latest:
            out["latest_backup"] = {"path": str(latest[0].backup_path), "created_at": latest[0].created_at}
    except Exception:  # noqa: BLE001 - pointer only; never fail a snapshot over it
        pass
    return out


def _redact_dsn(dsn: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "<redacted>"
    if not parts.password:
        return dsn
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit(parts._replace(netloc=f"{parts.username}:***@{host}"))


def dump_database(
    dsn: str,
    out_dir: Path,
    *,
    yes_this_database: bool = False,
    label: str = "state-snapshot",
    _dry_run: bool = False,
) -> dict[str, Any]:
    """A thin guard over :func:`axiom.infra.backup.create_backup`.

    It only ever dumps the DSN it is handed (never resolves one itself), and
    refuses the default developer address (localhost:5432) unless the caller
    says ``yes_this_database`` -- that address is where a developer's live
    database lives, and dumping it by accident is how a laptop fills up.
    """
    from urllib.parse import urlsplit

    from axiom.infra.backup import create_backup

    if not dsn:
        raise SnapshotError("no database URL configured; pass --db-url (nothing was dumped)")
    parts = urlsplit(dsn)
    addr = ((parts.hostname or "").lower(), parts.port or 5432)
    if addr in DEFAULT_DEV_DB_ADDRESSES and not yes_this_database:
        raise SnapshotError(
            f"refusing to dump the default developer database address {addr[0] or '(local socket)'}:"
            f"{addr[1]}; if that really is the database you mean, add --yes-this-database"
        )
    out_dir = Path(out_dir)
    bc.mkdir_private(out_dir)
    result = create_backup(dsn, output_dir=out_dir, label=label, fmt="custom", _dry_run=_dry_run)
    if not result.success:
        raise SnapshotError(f"database dump failed: {(result.error or '').strip()[:300]}")
    with contextlib.suppress(OSError):
        os.chmod(result.backup_path, 0o600)
    return {
        "relpath": str(Path(result.backup_path).relative_to(out_dir.parent)),
        "sha256": bc.sha256_file(Path(result.backup_path)),
        "size": result.size_bytes,
        "fmt": "custom",
        "database": _redact_dsn(dsn),
    }


def create_snapshot(
    state_dir: Path | None = None,
    root: Path | None = None,
    *,
    reason: str = "on-demand",
    trigger: str = "manual",
    policy: SnapshotPolicy | None = None,
    force: bool = False,
    with_db: bool = False,
    db_url: str | None = None,
    yes_this_database: bool = False,
    prune: bool | None = None,
    _db_dry_run: bool = False,
    _prune_under_pressure: bool = True,
) -> SnapshotResult:
    """Take a snapshot.  Skips (``skipped=True``) when nothing changed.

    Raises :class:`DiskPressureError` rather than fill the disk, and :class:`SnapshotError` (with
    the existing snapshots untouched) if a write fails part-way."""
    state_dir = Path(state_dir) if state_dir else _default_state_dir()
    root = Path(root) if root else default_snapshot_root(state_dir)
    policy = policy or load_policy(state_dir)
    store = open_store(state_dir, root)
    res = SnapshotResult(ok=True, root=root)
    warn = bc.same_device_warning(
        root, state_dir, "Use --to <dir on another disk> (or set a second copy) for real protection.",
        what="snapshot dir", of="the state dir")
    if warn:
        res.warnings.append(warn)

    scan = scan_state(state_dir, policy)
    res.scan_errors = scan.errors
    res.excluded_summary = scan.excluded
    inputs = _inputs_hash(scan.included)

    with store.locked():
        latest_id = next(iter(sorted(store.ids(), reverse=True)), None)
        latest_m: dict[str, Any] = {}
        if latest_id:
            with contextlib.suppress(SnapshotError):
                latest_m = store.read_manifest(latest_id)
        if (
            not force
            and not with_db
            and latest_m.get("body", {}).get("inputs_hash") == inputs
        ):
            res.skipped = True
            res.snapshot_id = latest_id
            res.message = f"unchanged since {latest_id}; no new snapshot taken"
            res.files = len(scan.included)
            return res

        # --- space: only blobs not already stored cost anything -------------
        new_raw = {e.sha256: e.size for e in scan.included if not store.has_blob(e.sha256)}
        need = sum(new_raw.values())
        ok, why = _free_check(root, need, policy.retention)
        if not ok and not _prune_under_pressure:
            raise DiskPressureError(f"REFUSING to snapshot: {why}. Nothing was written.")
        if not ok:
            plan = prune_store(store, policy, pressure=True, apply=True, _locked=True)
            res.pruned.extend(plan.deleted)
            ok, why = _free_check(root, need, policy.retention)
            if not ok:
                raise DiskPressureError(
                    f"REFUSING to snapshot: {why}. Pruned {len(plan.deleted)} snapshot(s) under "
                    f"pressure and there is still not enough room. Free some space or point "
                    f"snapshots at another disk with --to <dir>."
                )
            res.warnings.append(f"low disk space: pruned {len(plan.deleted)} snapshot(s) harder than usual")

        # --- write blobs, then re-check each file did not change under us ----
        entries: list[dict[str, Any]] = []
        added = 0
        for e in scan.included:
            try:
                with _stable_copy(e.abs_path) as src:
                    data = src.read_bytes()
            except (OSError, SnapshotError) as exc:
                res.scan_errors.append(f"{e.path}: {exc}")
                continue
            sha = bc.sha256_bytes(data)  # the bytes we store are the bytes we hash
            if not store.has_blob(sha):
                try:
                    added += store.put_blob(sha, data)
                except OSError as exc:
                    raise _write_failed(exc, "a blob") from exc
            entries.append({"path": e.path, "sha256": sha, "size": len(data), "mode": e.mode, "tier": e.tier})
        inputs = _inputs_hash(entries)
        res.bytes_added = added
        res.bytes_logical = sum(x["size"] for x in entries)
        res.files = len(entries)

        now = bc.utcnow()
        sid = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{inputs[:8]}"
        n = 1
        while store.snap_dir(sid).exists():
            n += 1
            sid = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{inputs[:8]}-{n}"

        db_section = db_pointer(state_dir)
        if with_db:
            try:
                dump = dump_database(
                    db_url or "", root / "db" / sid, yes_this_database=yes_this_database,
                    _dry_run=_db_dry_run,
                )
                # keep the dump inside root so it is under the same permissions/lock
                dump["relpath"] = str(Path("db") / sid / Path(dump["relpath"]).name)
                db_section = {**db_section, "covered": True, "dump": dump}
            except SnapshotError:
                shutil.rmtree(root / "db" / sid, ignore_errors=True)
                raise

        body = {
            "version": SCHEMA_VERSION,
            "id": sid,
            "created_at": bc.iso(now),
            "host": socket.gethostname(),
            "axiom_version": _axiom_version(),
            "reason": reason,
            "trigger": trigger,
            "state_dir": str(state_dir),
            "inputs_hash": inputs,
            "files": sorted(entries, key=lambda x: x["path"]),
            "excluded": scan.excluded,
            "secret_metadata": scan.secret_metadata,
            "scan_errors": res.scan_errors,
            "db": db_section,
            "not_covered": [
                "database contents (see db)",
                "credential/keychain/vault VALUES (names only in secret_metadata)",
                "excluded files listed above",
            ],
        }
        prev = latest_m.get("chain_hash", "") if latest_m else ""
        if not prev:
            # chain to the ledger's newest snapshot even if its dir was pruned
            for le in reversed(store.ledger_entries()):
                if le.get("event") == "snapshot":
                    prev = le.get("detail", {}).get("chain_hash", "")
                    break
        body_sha, chain = bc.manifest_chain(prev, body)
        manifest = {"body": body, "prev": prev, "body_sha256": body_sha, "chain_hash": chain}

        # --- atomic publish: build elsewhere, fsync, rename ------------------
        stage = Path(tempfile.mkdtemp(dir=str(store.tmp), prefix=f"{sid}.partial-"))
        try:
            os.chmod(stage, 0o700)
            bc.atomic_publish(stage / "manifest.json", json.dumps(manifest, indent=1, sort_keys=True).encode())
            bc.fsync_dir(stage)
            bc.FAULTS.hit("backup.publish", id=sid)
            os.rename(stage, store.snap_dir(sid))
            bc.fsync_dir(store.snaps)
        except BaseException as exc:
            if not isinstance(exc, bc.SimulatedCrash):
                shutil.rmtree(stage, ignore_errors=True)
            if isinstance(exc, OSError):
                raise _write_failed(exc, "the manifest") from exc
            raise
        try:
            store.ledger_append("snapshot", sid, {
                "chain_hash": chain, "reason": reason, "trigger": trigger,
                "files": len(entries), "bytes_added": added,
            })
        except OSError as exc:
            # published but unrecorded would read as "added outside the tool": take it back
            shutil.rmtree(store.snap_dir(sid), ignore_errors=True)
            raise _write_failed(exc, "the ledger entry") from exc

        _publish_emergency_script(root)
        res.snapshot_id = sid
        res.db = db_section
        res.message = f"snapshot {sid}: {len(entries)} files, {res.bytes_logical} bytes ({added} newly stored)"
        if policy.auto_prune if prune is None else prune:
            plan = prune_store(store, policy, apply=True, _locked=True)
            res.pruned.extend(plan.deleted)
    return res


# ===========================================================================
# Emergency script
# ===========================================================================

EMERGENCY_SCRIPT = "axi-restore.py"


def emergency_script_path(root: Path | None = None) -> Path | None:
    """The stdlib-only restore script: the copy beside the snapshots, else the repo's."""
    cands: list[Path] = []
    if root is not None:
        cands.append(Path(root) / EMERGENCY_SCRIPT)
    try:
        from axiom import REPO_ROOT

        cands.append(Path(REPO_ROOT) / "scripts" / EMERGENCY_SCRIPT)
    except Exception:  # noqa: BLE001
        pass
    return next((c for c in cands if c.is_file()), None)


def _publish_emergency_script(root: Path) -> None:
    """Keep a copy of the stdlib restore script next to the snapshots.

    If the snapshots live on another disk, the means to restore them travels
    with them; if the Axiom install is what broke, the script is still there.
    """
    try:
        from axiom import REPO_ROOT

        src = Path(REPO_ROOT) / "scripts" / EMERGENCY_SCRIPT
        dest = Path(root) / EMERGENCY_SCRIPT
        if src.is_file() and (not dest.is_file() or dest.read_bytes() != src.read_bytes()):
            bc.atomic_publish(dest, src.read_bytes())
            os.chmod(dest, 0o700)
    except Exception as exc:  # noqa: BLE001 - convenience copy; never fail a snapshot
        log.debug("could not publish the emergency restore script: %s", exc)


# ===========================================================================
# Prune
# ===========================================================================


@dataclass
class PruneResult:
    plan: RetentionPlan
    applied: bool
    deleted: list[str] = field(default_factory=list)
    blobs_removed: int = 0
    bytes_freed: int = 0


def prune_store(
    store: Store,
    policy: SnapshotPolicy,
    *,
    apply: bool = False,
    pressure: bool = False,
    now: datetime | None = None,
    verify_first: bool = True,
    _locked: bool = False,
) -> PruneResult:
    """Plan (always) and, with ``apply``, execute retention.  Every decision is logged."""
    cm = contextlib.nullcontext() if _locked or not apply else store.locked()
    with cm:
        v = verify_store(store, deep=False) if verify_first else None
        infos = _snapshot_infos(store, v)
        plan = plan_retention(infos, policy.retention, now or bc.utcnow(), pressure=pressure)
        if apply and plan.delete and verify_first:
            # About to delete history: a quick check cannot see a bit-flipped blob, and "the newest
            # verified snapshot is kept" is only as good as the verification behind it.
            v = verify_store(store, deep=True)
            infos = _snapshot_infos(store, v)
            plan = plan_retention(infos, policy.retention, now or bc.utcnow(), pressure=pressure)
        out = PruneResult(plan=plan, applied=False)
        if not apply:
            return out
        for d in plan.delete:
            if store.is_pinned(d.id):  # belt and braces: the planner already keeps pinned
                continue
            store.ledger_append("prune", d.id, {"reasons": list(d.reasons), "pressure": pressure})
            shutil.rmtree(store.snap_dir(d.id), ignore_errors=True)
            shutil.rmtree(store.root / "db" / d.id, ignore_errors=True)
            out.deleted.append(d.id)
        out.applied = True
        if out.deleted:
            bc.fsync_dir(store.snaps)
            out.blobs_removed, out.bytes_freed = _gc_blobs(store)
    return out


def _gc_blobs(store: Store) -> tuple[int, int]:
    """Remove blobs no remaining snapshot references (and stale temp debris)."""
    live: set[str] = set()
    for sid in store.ids():
        try:
            live.update(f["sha256"] for f in _snapshot_files(store.read_manifest(sid)))
        except SnapshotError:
            return 0, 0  # cannot prove what is referenced: collect nothing
    removed = freed = 0
    if store.blobs.is_dir():
        for sub in store.blobs.iterdir():
            if not sub.is_dir():
                continue
            for b in sub.iterdir():
                if b.name not in live:
                    try:
                        freed += b.stat().st_size
                        b.unlink()
                        removed += 1
                    except OSError:
                        pass
    for stale in (store.tmp.iterdir() if store.tmp.is_dir() else []):
        with contextlib.suppress(OSError):
            if time.time() - stale.stat().st_mtime > 3600:
                shutil.rmtree(stale) if stale.is_dir() else stale.unlink()
    return removed, freed


def set_pinned(store: Store, sid: str, pinned: bool) -> None:
    if sid not in store.ids():
        raise SnapshotError(f"no snapshot {sid!r}")
    with store.locked():
        marker = store.snap_dir(sid) / "PINNED"
        if pinned:
            bc.atomic_publish(marker, b"pinned\n")
        else:
            marker.unlink(missing_ok=True)
        store.ledger_append("pin" if pinned else "unpin", sid, {})


# ===========================================================================
# Resolve / diff / restore
# ===========================================================================


def newest_first(store: Store) -> list[str]:
    """Snapshot ids, most recently TAKEN first.

    The ledger's sequence is the order things really happened; a wall clock can be wrong (a
    snapshot taken while the clock was set ahead must not stay "newest" forever).  Snapshots the
    ledger never recorded sort after every recorded one, then by their own timestamp, then id."""
    seq: dict[str, int] = {}
    for e in store.ledger_entries():
        if e.get("event") == "snapshot" and e.get("id") and isinstance(e.get("seq"), int):
            seq[e["id"]] = e["seq"]
    return sorted(store.ids(), key=lambda i: (seq.get(i, 0), _created(store, i), i), reverse=True)


#: A snapshot taken by a restore, to undo it.  It holds the state the restore found (often the
#: damage being repaired), so it is never what ``latest`` means; it is restored by naming it.
SAFETY_TRIGGER = "restore"


def is_safety_snapshot(store: Store, sid: str) -> bool:
    try:
        return store.read_manifest(sid)["body"].get("trigger") == SAFETY_TRIGGER
    except (SnapshotError, KeyError, AttributeError, TypeError):
        return False


def restorable_order(store: Store) -> list[str]:
    """``newest_first`` without the safety snapshots (unless nothing else exists)."""
    ordered = newest_first(store)
    regular = [i for i in ordered if not is_safety_snapshot(store, i)]
    return regular or ordered


def unavailable_reason(root: Path) -> str | None:
    """Why the snapshot directory cannot be used at all, in words an operator can act on."""
    return bc.directory_problem(root, "snapshot directory")


def _no_snapshots_message(store: Store) -> str:
    why = unavailable_reason(store.root)
    where = (f"{why}. " if why else f"no snapshots in {store.root}. ")
    return where + (f"If they are elsewhere, point at them (`{_brand_cli()} state restore --from DIR`) or use "
                    f"a second copy; if the CLI is unusable, run `python3 -I {EMERGENCY_SCRIPT} state list --snapshots DIR`.")


def resolve_id(store: Store, ref: str) -> str:
    """``latest``, ``latest-verified``, an exact id, or a unique id prefix."""
    try:
        ids = store.ids()
    except OSError:
        ids = []
    if not ids:
        raise SnapshotError(_no_snapshots_message(store))
    ordered = restorable_order(store)
    if ref == "latest":
        return ordered[0]
    if ref == "latest-verified":
        for sid in ordered:
            if verify_snapshot(store, sid, deep=True).ok:
                return sid
        raise SnapshotError("no snapshot passes verification")
    if ref in ids:
        return ref
    matches = [i for i in ids if i.startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    raise SnapshotError(
        f"{'ambiguous' if matches else 'unknown'} snapshot {ref!r}"
        + (f": {', '.join(matches)}" if matches else f"; try `{_brand_cli()} state ls`")
    )


def _created(store: Store, sid: str) -> str:
    try:
        return store.read_manifest(sid)["body"]["created_at"]
    except (SnapshotError, KeyError, TypeError):
        return ""


class SnapshotKind:
    """:class:`~axiom.infra.backup_core.BackupKind` over a snapshot store (deep checks)."""

    noun = "snapshot"

    def __init__(self, store: Store) -> None:
        self.store = store
        self._recorded: dict[str, str] | None = None
        self._seq: dict[str, int] = {}
        chain = store._chain.verify()
        self._first_bad = chain.first_bad
        self._first_bad_why = chain.problems[0] if chain.problems else ""

    def candidates(self) -> list[str]:
        return restorable_order(self.store)

    def _ledger_chain_hashes(self) -> dict[str, str]:
        if self._recorded is None:
            events = [e for e in self.store.ledger_entries() if e.get("event") == "snapshot" and e.get("id")]
            self._recorded = {e["id"]: e.get("detail", {}).get("chain_hash", "") for e in events}
            self._seq = {e["id"]: int(e.get("seq") or 0) for e in events}
        return self._recorded

    def check(self, bid: str) -> list[str]:
        problems = list(verify_snapshot(self.store, bid, deep=True).problems)
        recorded = self._ledger_chain_hashes()
        if self._first_bad is not None and self._seq.get(bid, 0) >= self._first_bad:
            problems.append(f"its ledger entry ({self._seq[bid]}) is past a break in the ledger chain "
                            f"(entry {self._first_bad}: {self._first_bad_why}), so the ledger cannot vouch for it")
        if recorded:
            if bid not in recorded:
                problems.append("snapshot is not in the ledger (added outside the tool?)")
            else:
                try:
                    if recorded[bid] != self.store.read_manifest(bid).get("chain_hash"):
                        problems.append("manifest differs from the one the ledger recorded (substituted)")
                except SnapshotError:
                    pass
        return problems


def _confine(state_dir: Path, rel: str) -> Path:
    """The absolute target for a manifest path, or raise if it escapes *state_dir*."""
    if not rel or rel.startswith("/") or "\\" in rel or "\0" in rel:
        raise SnapshotError(f"manifest path {rel!r} is not a safe relative path")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise SnapshotError(f"manifest path {rel!r} is not a safe relative path")
    base = os.path.realpath(state_dir)
    # The parent chain may not pass through a symlink that leaves the state dir.  The LAST component is
    # deliberately not followed: a restore replaces what is at that path (never writes through it), and
    # a symlink there is a conflict to report, not a reason to abandon the whole restore.
    parent = os.path.realpath(os.path.join(base, *parts[:-1]))
    if parent != base and not parent.startswith(base + os.sep):
        raise SnapshotError(f"manifest path {rel!r} resolves outside the state dir")
    return Path(parent, parts[-1])


#: Files a restore never rolls back while they exist. The audit chain is the record of what was done, and
#: rolling it back erases the restore's own refusals and breaks its hash chain; the approval queue holds
#: single-use approvals, and rolling it back would resurrect ones already spent. Both are still captured
#: by a snapshot, and a restore onto a machine that lacks them (a rebuilt disk) puts them back.
RESTORE_KEEPS: tuple[str, ...] = ("audit/**", "orchestrator/**")
_KEPT_NOTE = "live audit trail or approval queue: a restore never rolls these back"


def is_restore_kept(relpath: str) -> bool:
    return any(glob_match(g, relpath) for g in RESTORE_KEEPS)


@dataclass
class DiffEntry:
    path: str
    action: str  # create | replace | chmod | unchanged | conflict | kept
    tier: str
    snapshot_sha: str
    current_sha: str | None = None
    note: str = ""


def diff_snapshot(
    store: Store,
    sid: str,
    state_dir: Path,
    *,
    only: Sequence[str] = (),
) -> list[DiffEntry]:
    """What restoring *sid* would change in *state_dir*.  Read-only."""
    m = store.read_manifest(sid)
    out: list[DiffEntry] = []
    for f in _snapshot_files(m):
        rel = f["path"]
        if only and not any(glob_match(g, rel) for g in only):
            continue
        target = _confine(Path(state_dir), rel)
        tier = f.get("tier", "")
        if target.is_symlink():
            out.append(DiffEntry(rel, "conflict", tier, f["sha256"], None,
                                 "a symlink is in the way; restoring would replace the link itself"))
            continue
        if target.exists() and not target.is_file():
            out.append(DiffEntry(rel, "conflict", tier, f["sha256"], None,
                                 "something that is not a regular file is in the way (a directory?)"))
            continue
        if not target.is_file():
            out.append(DiffEntry(rel, "create", tier, f["sha256"], None))
            continue
        try:
            cur = bc.sha256_file(target)
        except OSError as exc:
            out.append(DiffEntry(rel, "conflict", tier, f["sha256"], None,
                                 f"the current file cannot be read ({exc.strerror or exc.errno})"))
            continue
        if is_restore_kept(rel):
            out.append(DiffEntry(rel, "kept" if cur != f["sha256"] else "unchanged", tier, f["sha256"], cur,
                                 _KEPT_NOTE if cur != f["sha256"] else ""))
        elif cur != f["sha256"]:
            out.append(DiffEntry(rel, "replace", tier, f["sha256"], cur))
        elif stat.S_IMODE(target.stat().st_mode) != f.get("mode"):
            out.append(DiffEntry(rel, "chmod", tier, f["sha256"], cur))
        else:
            out.append(DiffEntry(rel, "unchanged", tier, f["sha256"], cur))
    return out


@dataclass
class RestoreResult:
    ok: bool
    snapshot_id: str
    applied: bool
    dry_run: bool
    diff: list[DiffEntry] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    set_aside: dict[str, str] = field(default_factory=dict)
    #: path -> why it was NOT restored (conflicts left alone, writes that failed)
    not_restored: dict[str, str] = field(default_factory=dict)
    #: newer snapshots passed over by ``latest`` -> why
    skipped_snapshots: dict[str, list[str]] = field(default_factory=dict)
    requested: str = ""
    pre_restore_snapshot: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    stamp: str = ""


def _aside_name(target: Path, stamp: str) -> Path:
    cand = target.with_name(f"{target.name}.pre-restore-{stamp}")
    n = 1
    while cand.exists() or cand.is_symlink():
        n += 1
        cand = target.with_name(f"{target.name}.pre-restore-{stamp}-{n}")
    return cand


def _install_file(
    store: Store, f: dict[str, Any], target: Path, stamp: str, *, replace_link: bool = False
) -> Path | None:
    """Stage, set the current file aside (hardlink/copy: never absent), swap, re-hash.

    Returns the aside path (or None if there was no current file).  Every step either leaves
    *target* as it was or makes it exactly the snapshot's bytes; a write that did not land is
    detected by the re-hash and reported, never assumed."""
    sha = f["sha256"]
    data = store.read_blob(sha)
    if bc.sha256_bytes(data) != sha:
        raise SnapshotError(f"{f['path']}: blob does not match its hash; not restoring")
    _mkdir_private_like(target.parent)

    def write(out: Any) -> None:
        bc.FAULTS.account(len(data), path=str(target))
        out.write(data)

    tmp = bc.stage_file(target.parent, write, prefix=f".{target.name}.restore-", mode=f.get("mode", 0o600))
    aside: Path | None = None
    try:
        if target.is_symlink() and replace_link:
            aside = _aside_name(target, stamp)
            os.rename(target, aside)  # the link itself is kept, as a link
        elif target.exists() or target.is_symlink():
            aside = _aside_name(target, stamp)
            try:
                os.link(target, aside)
            except OSError:
                shutil.copy2(target, aside)
        bc.commit_staged(tmp, target)
    except BaseException as exc:
        bc.discard_staged(tmp, exc)
        if aside is not None and not isinstance(exc, bc.SimulatedCrash):
            if aside.is_symlink() and not target.exists() and not target.is_symlink():
                with contextlib.suppress(OSError):
                    os.rename(aside, target)
            else:
                with contextlib.suppress(OSError):
                    aside.unlink()
        raise
    if not _landed(target, sha):
        if aside is not None:
            with contextlib.suppress(OSError):
                os.replace(aside, target)  # the original comes back, whatever is there now
        raise SnapshotError(
            f"{f['path']}: the write did not land (post-restore hash check failed); the original was kept"
        )
    return aside


def _sweep_stale_restore_temps(state_dir: Path, rels: Sequence[str], older_than_s: float = 3600.0) -> int:
    """Remove staged files an interrupted restore left beside the files it touches (a power cut
    leaves them; an hour on, nothing can still be using one).  Returns how many were removed."""
    removed = 0
    for parent in {_confine(state_dir, r).parent for r in rels}:
        with contextlib.suppress(OSError):
            for p in parent.iterdir():
                if p.name.startswith(".") and ".restore-" in p.name and p.is_file() and not p.is_symlink():
                    if time.time() - p.stat().st_mtime > older_than_s:
                        p.unlink()
                        removed += 1
    return removed


def _landed(target: Path, sha: str) -> bool:
    """Did the bytes we staged actually become *target*?  Read back and re-hashed, never assumed."""
    return target.is_file() and not target.is_symlink() and bc.sha256_file(target) == sha


def _mkdir_private_like(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)


def restore_snapshot(
    state_dir: Path | None = None,
    root: Path | None = None,
    ref: str = "latest",
    *,
    only: Sequence[str] = (),
    apply: bool = False,
    force: bool = False,
    policy: SnapshotPolicy | None = None,
    _services: Callable[[Path], list[RunningService]] | None = None,
) -> RestoreResult:
    """Dry run unless ``apply``.  See the module docstring for the safety rules.

    ``latest`` means the newest snapshot that VERIFIES: a damaged newer one is passed over, and
    the result says which and why.  If none verifies, nothing changes and the errors list every
    reason with what to try next."""
    state_dir = Path(state_dir) if state_dir else _default_state_dir()
    root = Path(root) if root else default_snapshot_root(state_dir)
    store = open_store(state_dir, root)
    c = _brand_cli()
    result = RestoreResult(ok=True, snapshot_id="", applied=False, dry_run=not apply, requested=ref)

    head_problems = store.head_problems()
    chain = store._chain.verify()
    if ref == "latest":
        if head_problems and not force:
            result.ok = False
            result.errors += head_problems
            result.errors.append(
                "REFUSING to guess which snapshot is the latest from a store that may have been rolled back "
                "or emptied. Name one (`" + c + " state ls`, then `" + c + " state restore <id>`), or --force."
            )
            result.next_steps = [f"{c} state ls", f"{c} state verify"]
            return result
        sel = bc.select_newest_intact(SnapshotKind(store))
        if sel.chosen is None:
            if not store.ids():
                raise SnapshotError(_no_snapshots_message(store))
            result.skipped_snapshots = {r.id: r.reasons for r in sel.rejected}
            if not force:
                result.ok = False
                result.errors.append(
                    f"no snapshot in {root} passes verification, so nothing was changed. Why each failed:")
                for r in sel.rejected:
                    result.errors.append(f"  {r.id}: " + "; ".join(r.reasons[:3]))
                result.errors.append(
                    "Try: another location or a second copy (`" + c + " state restore --from DIR`), "
                    f"or the emergency script (`python3 -I {EMERGENCY_SCRIPT} state verify latest-verified "
                    "--snapshots DIR`), or --force to restore what is still intact from the newest.")
                result.next_steps = [f"{c} state verify", f"{c} state restore --from DIR",
                                     f"python3 -I {EMERGENCY_SCRIPT} state list --snapshots DIR"]
                return result
            sid = sel.rejected[0].id
            result.warnings.append("--force: no snapshot verifies; restoring what is intact from the newest")
        else:
            sid = sel.chosen
            if sel.fell_back:
                result.skipped_snapshots = {r.id: r.reasons for r in sel.rejected}
                result.warnings += [f"FELL BACK: {ln}" for ln in sel.explain("snapshot")]
    else:
        sid = resolve_id(store, ref)
        result.warnings += [f"WARNING: {p}" for p in [*head_problems, *chain.problems]]
    result.snapshot_id = sid

    result.diff = diff_snapshot(store, sid, state_dir, only=only)
    if only and not result.diff:
        result.ok = False
        result.errors.append(f"--only {list(only)} matched no file in {sid}")
        return result
    result.warnings += [f"CONFLICT {d.path}: {d.note}; it will be left alone"
                        + (" (--force replaces a symlink, keeping the link)" if "symlink" in d.note else "")
                        for d in result.diff if d.action == "conflict"]
    if not apply:
        return result

    v = verify_snapshot(store, sid, deep=True)
    if not v.ok and not force:
        result.ok = False
        result.errors.append(
            f"snapshot {sid} fails verification ({len(v.problems)} problem(s): {v.problems[0]}). "
            f"Refusing. Pick another with `{c} state ls`, or --force to restore what is intact."
        )
        return result
    if not v.ok:
        result.warnings.append(f"--force: restoring from a snapshot with {len(v.problems)} verification problem(s)")

    running = (_services or detect_running_services)(state_dir)
    if running and not force:
        result.ok = False
        result.errors.append(
            "Axiom services appear to be running and own these files: "
            + "; ".join(f"{r.what} ({r.detail})" for r in running[:5])
            + ". Stop them first, or --force."
        )
        return result
    if running:
        result.warnings.append("--force: restoring while services appear to be running")

    try:
        with bc.backup_lock(store.root / ".restore.lock", timeout=0.0,
                            busy=lambda: SnapshotBusy("another restore into this store is running"),
                            prepare=store.ensure):
            return _apply_restore(store, state_dir, root, sid, result, only=only, force=force, policy=policy)
    except SnapshotBusy as exc:
        result.ok = False
        result.errors.append(f"{exc}. Nothing was changed; wait for it to finish and retry.")
        return result


def _apply_restore(store: Store, state_dir: Path, root: Path, sid: str, result: RestoreResult, *,
                   only: Sequence[str], force: bool, policy: SnapshotPolicy | None) -> RestoreResult:
    c = _brand_cli()
    # pre-restore snapshot: refuse if it cannot be taken
    try:
        pre = create_snapshot(
            state_dir, root, reason=f"pre-restore of {sid}", trigger="restore", policy=policy,
            force=True, prune=False, _prune_under_pressure=False,
        )
        result.pre_restore_snapshot = pre.snapshot_id
    except (SnapshotError, OSError) as exc:
        if not force:
            result.ok = False
            result.errors.append(
                f"could not take the pre-restore snapshot ({exc}); REFUSING to restore without a safety net, "
                "nothing was changed. Free space or fix the store, or --force to restore without one.")
            return result
        result.warnings.append(f"--force: no pre-restore snapshot ({exc})")

    stamp = bc.utcnow().strftime("%Y%m%dT%H%M%SZ")
    result.stamp = stamp
    _sweep_stale_restore_temps(state_dir, [d.path for d in result.diff])
    m = store.read_manifest(sid)
    by_path = {f["path"]: f for f in _snapshot_files(m)}
    try:
        with store.locked():
            for d in result.diff:
                if d.action in ("unchanged", "kept"):
                    continue
                f = by_path[d.path]
                target = _confine(state_dir, d.path)
                if d.action == "conflict" and not (force and target.is_symlink()):
                    result.not_restored[d.path] = d.note
                    continue
                try:
                    bc.FAULTS.hit("restore.file", path=d.path)
                    if d.action == "chmod":
                        os.chmod(target, f.get("mode", 0o600) & 0o7777)
                        result.restored.append(d.path)
                        continue
                    aside = _install_file(store, f, target, stamp, replace_link=d.action == "conflict")
                    result.restored.append(d.path)
                    if aside is not None:
                        result.set_aside[d.path] = str(aside)
                except (SnapshotError, OSError) as exc:
                    result.not_restored[d.path] = str(exc)
            result.applied = True
            store.ledger_append("restore", sid, {
                "files": len(result.restored), "pre_restore_snapshot": result.pre_restore_snapshot,
                "errors": len(result.not_restored), "only": list(only),
            })
    except SnapshotBusy as exc:
        result.ok = False
        result.errors.append(f"{exc}. Nothing was changed.")
        return result
    if result.not_restored:
        result.ok = False
        for p, why in result.not_restored.items():
            result.errors.append(f"{p}: NOT restored: {why}")
        result.errors.append(
            f"PARTIAL RESTORE: {len(result.restored)} file(s) restored, {len(result.not_restored)} not (listed above). "
            "Every file is either its old or its new version, never torn. Fix the cause and re-run the same "
            "command: it only touches what still differs.")
        if result.pre_restore_snapshot:
            result.next_steps.append(f"{c} state restore {result.pre_restore_snapshot} --apply   (undo)")
    return result


# ===========================================================================
# Seams for other components (chaos-hardening fallback)
# ===========================================================================


def latest_snapshot_for(
    path: str | os.PathLike[str],
    *,
    state_dir: Path | None = None,
    root: Path | None = None,
) -> SnapshotRef | None:
    """Newest snapshot holding an intact copy of *path*, or ``None``.

    "Intact" is checked for the one file asked about (manifest hash chain and
    the blob's content hash), not the whole snapshot: a caller recovering a
    corrupt file must not pay for verifying 150 MB it does not want.
    """
    state_dir = Path(state_dir) if state_dir else _default_state_dir()
    root = Path(root) if root else default_snapshot_root(state_dir)
    rel = _relative_to_state(path, state_dir)
    store = open_store(state_dir, root)
    for sid in restorable_order(store):
        try:
            m = store.read_manifest(sid)
        except SnapshotError:
            continue
        body = m.get("body", {})
        if bc.manifest_chain(m.get("prev", ""), body)[0] != m.get("body_sha256"):
            continue
        for f in body.get("files", []):
            if f.get("path") == rel:
                if store.has_blob(f["sha256"]) and store.blob_ok(f["sha256"]):
                    return SnapshotRef(
                        sid, body.get("created_at", ""), rel, f["sha256"], f.get("size", 0),
                        f.get("mode", 0o600), snapshot_verified=True,
                    )
                break
    return None


def _relative_to_state(path: str | os.PathLike[str], state_dir: Path) -> str:
    p = Path(path)
    if p.is_absolute():
        base = os.path.realpath(state_dir)
        # realpath the parent only: the file itself may be missing or corrupt
        full = os.path.join(os.path.realpath(p.parent), p.name)
        if not (full == base or full.startswith(base + os.sep)):
            raise ValueError(f"{path} is not under the state dir {state_dir}")
        rel = os.path.relpath(full, base)
    else:
        rel = str(p)
    rel = rel.replace(os.sep, "/")
    _confine(state_dir, rel)  # raises on traversal
    return rel


def restore_file(
    path: str | os.PathLike[str],
    *,
    state_dir: Path | None = None,
    root: Path | None = None,
    snapshot: str | None = None,
) -> SnapshotRef:
    """Put the newest intact snapshot copy of *path* back, preserving the current file.

    Unlike :func:`restore_snapshot` this is meant to be called from inside a
    running component that just found its own file corrupt, so it does not
    refuse for running services: it replaces exactly one file, atomically, and
    keeps the corrupt one as ``<name>.pre-restore-<ts>`` for forensics.
    Raises :class:`SnapshotError` when no snapshot holds an intact copy.
    """
    state_dir = Path(state_dir) if state_dir else _default_state_dir()
    root = Path(root) if root else default_snapshot_root(state_dir)
    rel = _relative_to_state(path, state_dir)
    store = open_store(state_dir, root)
    ref = latest_snapshot_for(path, state_dir=state_dir, root=root)
    if snapshot:
        sid = resolve_id(store, snapshot)
        m = store.read_manifest(sid)
        f = next((x for x in _snapshot_files(m) if x["path"] == rel), None)
        if f is None or not store.blob_ok(f["sha256"]):
            raise SnapshotError(f"snapshot {sid} has no intact copy of {rel}")
        ref = SnapshotRef(sid, m["body"]["created_at"], rel, f["sha256"], f["size"], f["mode"], True)
    if ref is None:
        raise SnapshotError(f"no snapshot holds an intact copy of {rel}")
    m = store.read_manifest(ref.snapshot_id)
    f = next(x for x in _snapshot_files(m) if x["path"] == rel)
    stamp = bc.utcnow().strftime("%Y%m%dT%H%M%SZ")
    _install_file(store, f, _confine(state_dir, rel), stamp)
    return ref


# ===========================================================================
# snapshot_before: the guard around risky operations
# ===========================================================================


def take_snapshot_before(
    reason: str,
    *,
    state_dir: Path | None = None,
    root: Path | None = None,
    strict: bool = False,
    announce: Callable[[str], None] | None = None,
) -> SnapshotResult | None:
    """Snapshot ahead of a risky operation; the function form of :func:`snapshot_before`.

    For call sites where wrapping the operation in a ``with`` would re-indent
    a long function.  A failure to snapshot is logged LOUDLY and the operation
    proceeds (the operator asked for the upgrade, not for a snapshot) unless
    ``strict=True``, which raises.  ``AXIOM_SNAPSHOT_BEFORE=0`` disables it
    (the test suite sets this), and so does ``state.snapshots.enabled = false``.
    """
    say = announce or (lambda m: log.info("%s", m))
    if os.environ.get("AXIOM_SNAPSHOT_BEFORE", "1") in ("0", "false", "off"):
        return None
    try:
        sd = Path(state_dir) if state_dir else _default_state_dir()
        pol = load_policy(sd)
        if not pol.enabled:
            return None
        result = create_snapshot(sd, root, reason=reason, trigger="before", policy=pol)
        say(f"state snapshot before '{reason}': {result.message}")
        return result
    except Exception as exc:  # noqa: BLE001 - see docstring
        msg = f"COULD NOT take a state snapshot before '{reason}': {exc}"
        log.error(msg)
        say(msg)
        if strict:
            raise SnapshotError(msg) from exc
        return None


@contextlib.contextmanager
def snapshot_before(
    reason: str,
    *,
    state_dir: Path | None = None,
    root: Path | None = None,
    strict: bool = False,
    announce: Callable[[str], None] | None = None,
) -> Iterator[SnapshotResult | None]:
    """``with snapshot_before("ext install foo"): ...`` -- snapshot, then run the body."""
    yield take_snapshot_before(reason, state_dir=state_dir, root=root, strict=strict, announce=announce)


# ===========================================================================
# Convenience: store for a state dir
# ===========================================================================


def head_file(state_dir: Path) -> Path:
    """Where the newest snapshot-ledger entry is recorded, outside the store."""
    return Path(state_dir) / "secrets" / "snapshot-head.json"


def open_store(state_dir: Path | None = None, root: Path | None = None) -> Store:
    sd = Path(state_dir) if state_dir else _default_state_dir()
    return Store(Path(root) if root else default_snapshot_root(sd), head_file(sd))


def snapshot_dict(obj: Any) -> Any:
    """JSON-friendly view of the dataclasses above."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: snapshot_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return bc.iso(obj)
    if isinstance(obj, dict):
        return {k: snapshot_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [snapshot_dict(v) for v in obj]
    return obj
