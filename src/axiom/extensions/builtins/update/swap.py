# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Take an update by building it beside the running version, then switching one link.

A node that runs unattended, at a site whose people should not have to babysit
it, cannot update the way a developer does: upgrading the environment it is
running from leaves it half-old, half-new if anything fails, with nothing to
go back to. And a node left alone for months meets everything at once: power
lost mid-update, a full disk, a package index that is gone, a release that
needs a newer Python.

Each version gets its own virtual environment under ``<root>/venvs/<ver>``,
and ``<root>/current`` is a symlink to the one in use. An update:

1. refuses early if the disk cannot hold another environment;
2. records what it is about to do in ``<root>/pending.json``, so a start after
   power loss can finish or undo it (:func:`recover`);
3. builds the new environment beside the running one, at its final path
   (environments are not relocatable); a failed install changes nothing and
   leaves nothing behind;
4. marks it complete, then runs the checks inside it, before anything switches;
5. switches ``current`` with one rename, so a reader sees the old link or the
   new one and never neither;
6. runs the post-switch health checks through ``current``, and switches back
   if they fail.

The previous environment is kept, and so is any environment a running pass
holds (:func:`in_use`), so going back is one rename and a pass in flight never
loses the files it started from. Every outcome is appended to
``<root>/update-log.jsonl``; :func:`last_outcome` is what a heartbeat reports,
and :func:`needs_alert` says when retrying has gone on long enough to tell a
person.

:func:`decide` is the policy: ``auto-patch`` (the default), ``approve`` or
``hold``, and an optional maintenance window. It never chooses a downgrade.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

#: Environments kept besides ``current`` and any in use: the one to roll back to.
KEEP_PREVIOUS = 1

#: Fixes and security releases apply on their own in the maintenance window;
#: anything that adds features waits for a person. A node that is neglected
#: for months still receives every fix, which is the failure this default
#: exists to prevent.
DEFAULT_POLICY = "auto-patch"

#: Written inside an environment once its install finished. An environment
#: without it is a half-built one a lost update left behind.
COMPLETE = ".complete"

#: Free space required before building another environment, by default.
DEFAULT_MIN_FREE_BYTES = 600 * 1024 * 1024

_PENDING = "pending.json"
_IN_USE = ".in-use"


@dataclass(frozen=True)
class UpdateOutcome:
    # updated | rejected_before_switch | rolled_back | rolled_back_at_cutover | install_failed |
    # insufficient_space | python_too_old | unchanged
    status: str
    from_version: str | None
    to_version: str
    detail: str = ""
    at: str = ""


def current_version(root: Path) -> str | None:
    link = Path(root) / "current"
    if not link.is_symlink():
        return None
    return Path(os.readlink(link)).name


def _log_entries(root: Path) -> list[dict]:
    log = Path(root) / "update-log.jsonl"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def last_outcome(root: Path) -> dict | None:
    entries = _log_entries(root)
    return entries[-1] if entries else None


def stuck_since(root: Path, version: str) -> datetime | None:
    """When the current run of failed attempts at ``version`` began, or ``None``.

    Counts back from the latest attempt at that version; a success ends the run.
    """
    first: datetime | None = None
    for entry in reversed(_log_entries(root)):
        if entry.get("to_version") != version:
            continue
        if entry.get("status") in ("updated", "unchanged"):
            break
        if entry.get("at"):
            first = datetime.fromisoformat(entry["at"])
    return first


def needs_alert(root: Path, version: str, *, days: float, now: datetime | None = None) -> bool:
    """True once attempts at ``version`` have failed for longer than ``days``."""
    since = stuck_since(root, version)
    if since is None:
        return False
    return (now or datetime.now(UTC)) - since >= timedelta(days=days)


def _record(root: Path, outcome: UpdateOutcome) -> UpdateOutcome:
    stamped = UpdateOutcome(**{**asdict(outcome), "at": datetime.now(UTC).isoformat()})
    with (Path(root) / "update-log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(stamped)) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return stamped


def _bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def _run_checks(venv: Path, checks: list[list[str]], timeout: float) -> str | None:
    """Run each check with the environment's executables first on PATH. ``None`` if all pass."""
    env = dict(os.environ, PATH=f"{_bin(venv)}{os.pathsep}{os.environ.get('PATH', '')}", VIRTUAL_ENV=str(venv))
    for cmd in checks:
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"`{' '.join(cmd)}` could not run: {exc}"
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
            return f"`{' '.join(cmd)}` exited {r.returncode}: {tail[0]}"
    return None


def _point(root: Path, version: str) -> None:
    """Point ``current`` at ``venvs/<version>`` with one atomic rename."""
    tmp = root / f".current.{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(Path("venvs") / version, tmp)
    os.replace(tmp, root / "current")
    _fsync_dir(root)


def _fsync_dir(path: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _begin(root: Path, from_version: str | None, to_version: str) -> None:
    p = Path(root) / _PENDING
    p.write_text(json.dumps({"from": from_version, "to": to_version}), encoding="utf-8")
    _fsync_dir(Path(root))


def _end(root: Path) -> None:
    (Path(root) / _PENDING).unlink(missing_ok=True)


def recover(root: Path, *, post_checks: list[list[str]], timeout: float = 300.0) -> UpdateOutcome | None:
    """Finish or undo an update a crash interrupted. Run once at start-up.

    ``None`` when nothing was pending. Otherwise: if ``current`` already points
    at a complete new version, run the post-switch checks and keep it or switch
    back; if the switch never happened, remove the half-built environment.
    """
    root = Path(root)
    for stray in root.glob(".current.*"):
        stray.unlink(missing_ok=True)
    pending = root / _PENDING
    if not pending.is_file():
        return None
    try:
        state = json.loads(pending.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    old, new = state.get("from"), state.get("to") or ""
    cur = current_version(root)
    new_dir = root / "venvs" / new if new else None
    if cur == new and new_dir is not None and (new_dir / COMPLETE).exists():
        failure = _run_checks(root / "current", post_checks, timeout)
        if failure and old:
            _point(root, old)
            out = UpdateOutcome("rolled_back", old, new, f"after an interrupted switch: {failure}")
        else:
            out = UpdateOutcome("updated", old, new, "completed after an interrupted switch")
    else:
        if cur != old and old:
            _point(root, old)
        if new_dir is not None and cur != new:
            shutil.rmtree(new_dir, ignore_errors=True)
        out = UpdateOutcome("rolled_back", old, new, "the update was interrupted before the switch")
    _end(root)
    return _record(root, out)


@contextlib.contextmanager
def in_use(root: Path) -> Iterator[Path]:
    """Hold the current environment for the length of a pass, so no update prunes it."""
    root = Path(root)
    version = current_version(root)
    marker = None
    if version:
        d = root / "venvs" / version / _IN_USE
        d.mkdir(parents=True, exist_ok=True)
        marker = d / str(os.getpid())
        marker.write_text("")
    try:
        yield root / "current"
    finally:
        if marker is not None:
            marker.unlink(missing_ok=True)


def _held(venv: Path) -> bool:
    d = venv / _IN_USE
    if not d.is_dir():
        return False
    for m in d.iterdir():
        try:
            pid = int(m.name)
        except ValueError:
            m.unlink(missing_ok=True)
            continue
        if _pid_alive(pid):
            return True
        m.unlink(missing_ok=True)
    return False


def _pid_alive(pid: int) -> bool:
    """Whether a process exists, without signalling it.

    Never ``os.kill(pid, 0)`` on Windows: there signal 0 is CTRL_C_EVENT, so
    the check interrupts the process it is checking.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def prune(root: Path, keep: set[str] | None = None) -> None:
    """Remove old environments: never ``current``, the previous one, or one a pass holds."""
    root = Path(root)
    venvs = root / "venvs"
    if not venvs.is_dir():
        return
    keep = set(keep or ())
    cur = current_version(root)
    if cur:
        keep.add(cur)
    for entry in reversed(_log_entries(root)):
        if entry.get("status") == "updated" and entry.get("to_version") == cur and entry.get("from_version"):
            keep.add(entry["from_version"])
            break
    for p in venvs.iterdir():
        if not p.is_dir() or p.name.startswith(".") or p.name in keep or _held(p):
            continue
        shutil.rmtree(p, ignore_errors=True)


def _needed_python(text: str) -> str | None:
    """The Requires-Python specifier pip quoted, e.g. ``>=3.13``."""
    import re

    # pip says either "... not in '>=3.13'" or "... Requires-Python >=3.13".
    m = re.search(r"not in '([^']+)'", text) or re.search(r"Requires-Python\s*'?([<>=!~][^'\s;]+)", text)
    return m.group(1) if m else None


def _install_hint(spec: str | None) -> str:
    """A version ``uv python install`` accepts that satisfies ``spec`` (best effort)."""
    import re

    m = re.search(r"(>=|>|==|~=)?\s*(\d+)(?:\.(\d+))?", spec or "")
    if not m:
        return "3.12"
    op, major, minor = m.group(1) or ">=", int(m.group(2)), m.group(3)
    if minor is None:
        return str(major + (1 if op == ">" else 0))
    return f"{major}.{int(minor) + (1 if op == '>' else 0)}"


def _python_too_old(text: str) -> bool:
    low = text.lower()
    return "requires a different python" in low or "requires-python" in low


def apply_update(
    root: Path,
    spec: str,
    version: str,
    *,
    checks: list[list[str]],
    post_checks: list[list[str]] | None = None,
    find_links: Path | None = None,
    index_url: str | None = None,
    python: str | None = None,
    timeout: float = 300.0,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    cutover: Callable[[Path], str | None] | None = None,
) -> UpdateOutcome:
    """Install ``spec`` as ``version`` beside the running version, check it, switch, verify.

    ``checks`` run in the new environment before the switch; ``post_checks``
    (default: the same checks) run through ``current`` after it, and a failure
    there switches back. ``python`` names the interpreter for the new
    environment, which is how a release needing a newer Python is taken.

    ``cutover(new_env)`` hands the running service over to the new version
    before ``current`` moves, with no downtime: a running collector starts the
    new version beside the old one, proves it live, then stops the old one
    (an overlap, never a restart). It returns ``None`` on success or why it
    rolled back; then the old version is still the one running and ``current``
    never moved.
    """
    root = Path(root)
    (root / "venvs").mkdir(parents=True, exist_ok=True)
    recover(root, post_checks=post_checks if post_checks is not None else checks, timeout=timeout)
    previous = current_version(root)
    if previous == version:
        return _record(root, UpdateOutcome("unchanged", previous, version, "already running"))

    free = shutil.disk_usage(root).free
    if free < min_free_bytes:
        return _record(root, UpdateOutcome(
            "insufficient_space", previous, version,
            f"{free // (1024 * 1024)} MB free, {min_free_bytes // (1024 * 1024)} MB needed; "
            "free some space and it is retried at the next window"))

    # ADR-182 D5a: recorded before anything is installed, so an outage that
    # overlaps this update, including one where the process dies mid-switch,
    # is attributed to us rather than left unexplained. A refusal above
    # (unchanged, no space) changes nothing and is not a change.
    from axiom.infra.change_intent import record_change

    with record_change("update", f"{previous or 'none'} -> {version}", detail=spec) as change:
        outcome = _install_and_switch(
            root, spec, version, previous, checks=checks, post_checks=post_checks,
            find_links=find_links, index_url=index_url, python=python, timeout=timeout,
            cutover=cutover,
        )
        change.outcome = outcome.status
        change.detail = outcome.detail
    return outcome


def _install_and_switch(
    root: Path,
    spec: str,
    version: str,
    previous: str | None,
    *,
    checks: list[list[str]],
    post_checks: list[list[str]] | None,
    find_links: Path | None,
    index_url: str | None,
    python: str | None,
    timeout: float,
    cutover: Callable[[Path], str | None] | None,
) -> UpdateOutcome:
    # Built at its final path: a virtual environment is not relocatable (its
    # scripts' shebangs name the path it was created at). Nothing points
    # `current` here until its checks pass, and pending.json lets a start
    # after power loss remove a half-built one.
    target = root / "venvs" / version
    shutil.rmtree(target, ignore_errors=True)
    _begin(root, previous, version)
    py = python or sys.executable
    try:
        subprocess.run([py, "-m", "venv", str(target)], check=True, capture_output=True, text=True, timeout=timeout)
        pip = [str(_bin(target) / "python"), "-m", "pip", "install", "--disable-pip-version-check", "--quiet"]
        if find_links is not None:
            pip += ["--no-index", "--find-links", str(find_links)]
        elif index_url:
            pip += ["--index-url", index_url]
        r = subprocess.run(pip + [spec], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            text = (r.stderr or r.stdout or "").strip()
            shutil.rmtree(target, ignore_errors=True)
            _end(root)
            if _python_too_old(text):
                needed = _needed_python(text)
                return _record(root, UpdateOutcome(
                    "python_too_old", previous, version,
                    f"{spec} needs Python {needed or 'newer than ' + py}. Install it without root "
                    f"with `uv python install {_install_hint(needed)}` and update again naming it; "
                    f"{previous} keeps running meanwhile."))
            return _record(root, UpdateOutcome(
                "install_failed", previous, version,
                f"install of {spec} failed: {text.splitlines()[-1:] or ['pip failed']}"))
    except (subprocess.SubprocessError, OSError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        _end(root)
        return _record(root, UpdateOutcome("install_failed", previous, version, f"install of {spec} failed: {exc}"))
    (target / COMPLETE).write_text(version, encoding="utf-8")

    failure = _run_checks(target, checks, timeout)
    if failure:
        shutil.rmtree(target, ignore_errors=True)
        _end(root)
        return _record(root, UpdateOutcome("rejected_before_switch", previous, version, failure))

    if cutover is not None:
        why = cutover(target)
        if why:
            shutil.rmtree(target, ignore_errors=True)
            _end(root)
            return _record(root, UpdateOutcome("rolled_back_at_cutover", previous, version, why))

    _point(root, version)

    failure = _run_checks(root / "current", post_checks if post_checks is not None else checks, timeout)
    if failure:
        if previous:
            _point(root, previous)
        _end(root)
        return _record(root, UpdateOutcome("rolled_back", previous, version, f"after the switch: {failure}"))

    _end(root)
    out = _record(root, UpdateOutcome("updated", previous, version))
    prune(root, keep={version} | ({previous} if previous else set()))
    return out


def _parse(v: str) -> tuple[int, ...]:
    out = []
    for part in v.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def decide(
    policy: str,
    current: str,
    candidate: str,
    *,
    approved: bool,
    now: datetime | None = None,
    window: tuple[time, time] | None = None,
) -> str:
    """``apply``, ``ask`` (someone must approve), ``wait`` (outside the window) or ``hold``."""
    if policy == "hold" or _parse(candidate) <= _parse(current):
        return "hold"
    cur, cand = _parse(current), _parse(candidate)
    patch_only = cur[:2] == cand[:2]
    if not approved and not (policy == "auto-patch" and patch_only):
        return "ask"
    if window is not None:
        start, end = window
        t = (now or datetime.now()).time()
        inside = start <= t < end if start <= end else (t >= start or t < end)
        if not inside:
            return "wait"
    return "apply"


__all__ = [
    "COMPLETE",
    "DEFAULT_POLICY",
    "UpdateOutcome",
    "apply_update",
    "current_version",
    "decide",
    "in_use",
    "last_outcome",
    "needs_alert",
    "prune",
    "recover",
    "stuck_since",
]
