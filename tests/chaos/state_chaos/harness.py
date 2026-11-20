# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reusable chaos harness for persisted harness state.

Three parts:

* :class:`ChaosRoot` -- a fresh temp tree plus the environment a subprocess
  needs so that EVERY state path resolves inside it. Nothing here may touch
  the developer's real ``~/.axi``; :func:`assert_chaos_path` is the guard and
  every mutating helper calls it first.
* :func:`damage` -- the library of ways a file or directory goes wrong.
* :func:`run_probe` / :func:`classify` -- run the REAL reader in a subprocess
  with that environment and sort what happened into
  ``recovered`` / ``degraded-loudly`` / ``silent-default`` / ``crash``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

UNREACHABLE_DB_URL = "postgresql+psycopg2://chaos:chaos@127.0.0.1:1/chaos_never_exists"
#: The source tree probes import. CHAOS_SRC points the matrix at any other checkout
#: (the findings report runs it against the unfixed base to show before/after).
SRC = Path(os.environ.get("CHAOS_SRC") or Path(__file__).resolve().parents[3] / "src")
PROBE_TIMEOUT_S = 60.0

#: ``recovered``       works, state defaulted/rebuilt, and a warning names the file.
#: ``degraded-loudly`` refuses with an error that names the file and a fix.
#: ``silent-default``  works with defaults and says nothing (right for an
#:                     absent optional file, a finding for damaged user data).
#: ``crash``           traceback / hang / unnamed error / semantic violation.
RECOVERED = "recovered"
DEGRADED = "degraded-loudly"
SILENT = "silent-default"
CRASH = "crash"


class ChaosGuardError(RuntimeError):
    """A chaos operation was aimed outside the disposable tree."""


def _real_homes() -> list[Path]:
    homes = []
    try:
        homes.append(Path(os.path.expanduser("~")).resolve())
    except RuntimeError:
        pass
    pw = os.environ.get("CHAOS_REAL_HOME")
    if pw:
        homes.append(Path(pw).resolve())
    return homes


def assert_chaos_path(path: Path | str, root: Path | str) -> Path:
    """Refuse unless ``path`` resolves strictly inside ``root`` and ``root`` is temp.

    ``root`` must itself live under the system temp directory (or pytest's
    ``tmp_path`` basetemp, which is) and must not be, contain, or sit inside
    the developer's real home state (``~/.axi``, ``~/.config``).
    """
    root_r = Path(root).resolve()
    path_r = Path(os.path.abspath(path))  # do not follow the target if it is a symlink
    parent_r = path_r.parent.resolve()
    target = parent_r / path_r.name
    tmp_roots = {Path(tempfile.gettempdir()).resolve(), Path("/private/tmp"), Path("/tmp")}
    tmp_roots |= {Path("/private/var/folders"), Path("/var/folders")}
    if not any(root_r == t or t in root_r.parents for t in tmp_roots):
        raise ChaosGuardError(f"chaos root {root_r} is not under a temp directory")
    for home in _real_homes():
        for guarded in (home / ".axi", home / ".config", home / ".ssh", home / ".claude"):
            if root_r == guarded or guarded in root_r.parents or root_r in guarded.parents:
                raise ChaosGuardError(f"chaos root {root_r} overlaps real state {guarded}")
    if target == root_r or root_r not in target.parents:
        raise ChaosGuardError(f"{target} is not strictly inside chaos root {root_r}")
    return target


@dataclass
class ChaosRoot:
    """A populated, disposable state tree and the env that points at it."""

    root: Path
    home: Path = field(init=False)
    state: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.home = self.root / "home"
        self.state = self.home / ".axi"
        assert_chaos_path(self.state, self.root)
        self.state.mkdir(parents=True, exist_ok=True)
        (self.root / "cwd").mkdir(exist_ok=True)
        (self.root / "xdg").mkdir(exist_ok=True)

    def path(self, rel: str) -> Path:
        return assert_chaos_path(self.state / rel, self.root)

    def env(self) -> dict[str, str]:
        """A scrubbed environment: nothing inherited except what is needed to run."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(SRC),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(self.home),
            "AXI_HOME": str(self.state),
            "AXI_STATE_DIR": str(self.state),
            "AXIOM_STATE_DIR": str(self.state),
            "XDG_CONFIG_HOME": str(self.root / "xdg"),
            "AXIOM_ACTION_LEDGER_BACKEND": "jsonl",
            "AXIOM_FOREIGN_SECRETS_BACKEND": "file",
            "AXIOM_MODE": "dev",
            # The default address (localhost:5432) is a developer's LIVE database.
            # Point every database consumer at a port nothing listens on so a
            # probe that forgets the in-memory seam fails loudly instead of
            # writing to real data.
            "AXIOM_DB_URL": UNREACHABLE_DB_URL,
            "AXIOM_TELEMETRY": "0",
            "AXIOM_ROOT": str(self.root / "cwd"),
            "COLUMNS": "80",
            "NO_COLOR": "1",
        }
        for k in ("AXI_HOME", "AXI_STATE_DIR", "AXIOM_STATE_DIR", "HOME"):
            assert_chaos_path(Path(env[k]) / "x", self.root)
        return env


# ---------------------------------------------------------------------------
# Damage library
# ---------------------------------------------------------------------------

FILE_DAMAGE = (
    "delete_file", "rename_away", "truncate_half", "zero_length", "garbage_bytes",
    "wrong_schema", "future_version", "perm_000", "file_to_dir", "dangling_symlink",
    "partial_write",
)
DIR_DAMAGE = ("delete_dir", "rename_away", "perm_000", "dir_to_file", "dangling_symlink")

GARBAGE = b"\x00\xff\xfe{[not json\x80\x81 \xde\xad\xbe\xef" * 4
WRONG_SCHEMA_DEFAULT = b'"this is valid JSON of the wrong shape"'


def damage(kind: str, path: Path, root: Path, *, wrong_schema: bytes | None = None) -> str:
    """Apply one damage ``kind`` to ``path``. Returns a human description.

    Every call is guarded: ``path`` must be strictly inside ``root`` and ``root``
    must be a temp tree. No variable reaches a destructive call unchecked.
    """
    target = assert_chaos_path(path, root)
    if kind in ("delete_file", "delete_dir"):
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    elif kind == "rename_away":
        os.rename(target, target.with_name(target.name + ".renamed-away"))
    elif kind == "truncate_half":
        data = target.read_bytes()
        target.write_bytes(data[: max(1, len(data) // 2)])
    elif kind == "zero_length":
        target.write_bytes(b"")
    elif kind == "garbage_bytes":
        target.write_bytes(GARBAGE)
    elif kind == "wrong_schema":
        target.write_bytes(wrong_schema if wrong_schema is not None else WRONG_SCHEMA_DEFAULT)
    elif kind == "future_version":
        # A newer release wrote this: same file, a schema version we do not know.
        suffix = target.suffix
        if suffix == ".json":
            try:
                doc = json.loads(target.read_text(encoding="utf-8"))
            except ValueError:
                doc = {}
            if isinstance(doc, dict):
                doc["version"] = 999
                doc["schema_version"] = 999
            target.write_text(json.dumps(doc), encoding="utf-8")
        elif suffix == ".toml":
            with target.open("a", encoding="utf-8") as fh:
                fh.write("\nschema_version = 999\n")
        elif suffix == ".jsonl":
            with target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"version": 999, "schema_version": 999}) + "\n")
        # opaque formats (keys, pem) carry no version: left untouched.
    elif kind == "perm_000":
        os.chmod(target, 0o000)
    elif kind == "file_to_dir":
        target.unlink()
        target.mkdir()
    elif kind == "dir_to_file":
        shutil.rmtree(target)
        target.write_bytes(b"i used to be a directory\n")
    elif kind == "dangling_symlink":
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        os.symlink(str(Path(root) / "does-not-exist-anywhere"), target)
    elif kind == "partial_write":
        # A writer killed mid-write leaves its temp file; the real file is intact.
        data = target.read_bytes()
        target.with_name(f".{target.name}.killed.tmp").write_bytes(data[: len(data) // 2])
        target.with_name(target.name + ".tmp").write_bytes(data[: len(data) // 3])
    else:  # pragma: no cover
        raise ValueError(f"unknown damage kind {kind!r}")
    return kind


def restore_permissions(root: Path) -> None:
    """Undo ``perm_000`` so the tree can be removed. Guarded to the temp root."""
    root = Path(root)
    assert_chaos_path(root / "x", root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            try:
                if not p.is_symlink():
                    os.chmod(p, 0o700 if p.is_dir() else 0o600)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Probe running + classification
# ---------------------------------------------------------------------------

PRELUDE = r'''
import json, logging, sys, traceback, io
_buf = io.StringIO()
_h = logging.StreamHandler(_buf)
_h.setLevel(logging.DEBUG)
logging.getLogger().addHandler(_h)
logging.getLogger().setLevel(logging.WARNING)
_RESULT = {"outcome": "ok", "value": None}
def emit(value=None, **extra):
    _RESULT["value"] = value
    _RESULT.update(extra)
def _finish():
    _RESULT["log"] = _buf.getvalue()
    sys.stdout.write("\n@@CHAOS@@" + json.dumps(_RESULT, default=str) + "\n")
    sys.stdout.flush()
try:
'''

EPILOGUE = r'''
except BaseException as _e:
    _RESULT["outcome"] = "error"
    _RESULT["exc_type"] = type(_e).__name__
    _RESULT["exc_msg"] = str(_e)
    _RESULT["traceback"] = traceback.format_exc()
_finish()
'''


def wrap_probe(code: str) -> str:
    body = "\n".join("    " + line for line in code.strip("\n").splitlines())
    return PRELUDE + body + "\n" + EPILOGUE


@dataclass
class ProbeResult:
    outcome: str  # ok | error | timeout | died
    value: object = None
    log: str = ""
    exc_type: str = ""
    exc_msg: str = ""
    stderr: str = ""
    returncode: int | None = 0
    traceback: str = ""


def run_probe(chaos: ChaosRoot, code: str, *, timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """Run ``code`` (a Python snippet) in a subprocess inside the chaos env."""
    script = wrap_probe(code)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            env=chaos.env(),
            cwd=str(chaos.root / "cwd"),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        return ProbeResult(outcome="timeout", stderr=str(exc.stderr or "")[-500:], returncode=None)
    marker = "@@CHAOS@@"
    if marker not in proc.stdout:
        return ProbeResult(outcome="died", stderr=proc.stderr[-2000:], returncode=proc.returncode)
    payload = json.loads(proc.stdout.rsplit(marker, 1)[1].strip())
    return ProbeResult(
        outcome=payload["outcome"],
        value=payload.get("value"),
        log=payload.get("log", ""),
        exc_type=payload.get("exc_type", ""),
        exc_msg=payload.get("exc_msg", ""),
        traceback=payload.get("traceback", ""),
        stderr=proc.stderr[-2000:],
        returncode=proc.returncode,
    )


FIX_WORDS = (
    "restore", "move", "rename", "remove", "delete", "repair", "recover", "run `", "run '",
    "axi ", "re-run", "rerun", "re-create", "recreate", "fix", "replace", "quarantin",
    "preserved at", ".corrupt-", "snapshot",
)


def names_file(text: str, path_hint: Path | str) -> bool:
    hint = Path(path_hint)
    return str(hint) in text or hint.name in text


def classify(res: ProbeResult, path_hint: Path | str) -> tuple[str, str]:
    """Sort a probe result. Returns ``(class, why)``."""
    if res.outcome == "timeout":
        return CRASH, "hang (timeout)"
    if res.outcome == "died":
        return CRASH, f"process died rc={res.returncode}: {res.stderr.strip().splitlines()[-1:]}"
    if res.outcome == "error":
        text = res.exc_msg or ""
        named = names_file(text, path_hint)
        fix = any(w in text.lower() for w in FIX_WORDS)
        if named and fix:
            return DEGRADED, f"{res.exc_type}: names file and a fix"
        if named:
            return CRASH, f"{res.exc_type}: names the file but offers no fix command"
        return CRASH, f"{res.exc_type}: error does not name the file ({text[:100]!r})"
    # ok
    if names_file(res.log, path_hint) or names_file(res.stderr, path_hint):
        return RECOVERED, "works; a warning names the file"
    return SILENT, "works; nothing was said"


def wait_for(predicate, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def run_concurrent(
    chaos: ChaosRoot, writer: str, reader: str, *, timeout: float = 90.0
) -> tuple[ProbeResult, ProbeResult]:
    """Run a writer loop and a reader loop at the same time against one tree."""
    procs = []
    for code in (writer, reader):
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", wrap_probe(code)],
                env=chaos.env(),
                cwd=str(chaos.root / "cwd"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
            )
        )
    out: list[ProbeResult] = []
    for proc in procs:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            out.append(ProbeResult(outcome="timeout", returncode=None))
            continue
        marker = "@@CHAOS@@"
        if marker not in stdout:
            out.append(ProbeResult(outcome="died", stderr=stderr[-2000:], returncode=proc.returncode))
            continue
        payload = json.loads(stdout.rsplit(marker, 1)[1].strip())
        out.append(
            ProbeResult(
                outcome=payload["outcome"], value=payload.get("value"), log=payload.get("log", ""),
                exc_type=payload.get("exc_type", ""), exc_msg=payload.get("exc_msg", ""),
                traceback=payload.get("traceback", ""), stderr=stderr[-2000:],
                returncode=proc.returncode,
            )
        )
    return out[0], out[1]
