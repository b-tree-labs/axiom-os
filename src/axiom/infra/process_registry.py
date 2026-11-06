# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Process registry: an ownership record for every process the harness starts.

Why this exists
---------------
A process the platform cannot name is a process the platform cannot stop. The
pre-push hook's CLI-subprocess tests left ``axiom_cli.py`` children spinning
at 100% CPU on Python 3.14 because nothing recorded that those children were
ours; the only remedy on offer was ``pkill -f``, which matches by *text* and
so can hit a developer's editor, another session, or a running node.

This module is the other half of that remedy. Everything the harness spawns
(tool subprocesses, agent runs, background tasks, services, MCP servers)
writes one small JSON record: pid, START TIME, owner, purpose, parent,
expected maximum runtime, and resource budgets. Anything that later signals a
process must go through :func:`verify_ours`, which re-checks the live process
against its record. PID reuse is the failure that makes "kill the pid in the
file" dangerous, so the start time is part of the identity: a pid whose start
time disagrees with the record is somebody else's process and is never touched.

Two identifiers, both ours
--------------------------
* The *record*: ``<state>/processes/records/<id>.json``, written atomically.
* The *mark*: the env var ``AXIOM_PROC_MARK=<scope>:<id>`` placed in the child's
  environment at spawn. Descendants inherit it, so a grandchild that escaped
  registration is still attributable, and a panic stop can find our processes
  even when the registry directory is damaged.

Only the mark is ever read back out of a process environment, and only by
pattern; environments hold credentials and are never retained or printed.

Nothing here signals anything. Detection lives in ``detect_rogues``;
containment (the part that sends signals) is in
:mod:`axiom.infra.process_containment` and is dry-run by default.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Env var carried by every process the harness registers (and its descendants).
MARK_ENV = "AXIOM_PROC_MARK"
#: Default mark scope. Tests use a unique scope so a scan can never match a
#: developer's real processes.
DEFAULT_SCOPE = "axiom"

_MARK_RE = re.compile(rf"{MARK_ENV}=([A-Za-z0-9_.\-]+):([0-9a-f]{{6,32}})")
_SECRETISH = re.compile(
    r"(?i)(--?(?:token|api[-_]?key|secret|password|passwd)[ =]|"
    r"\b(?:token|api[-_]?key|secret|password|passwd)=)\S+"
)

#: Start times come from ``ps`` at one-second resolution; allow a hair more
#: than that so two reads of the same process always agree, while a reused pid
#: (a different process, started later) almost never does.
START_TOLERANCE_S = 2.0

__all__ = [
    "DEFAULT_SCOPE",
    "MARK_ENV",
    "ProcInfo",
    "ProcessRecord",
    "ProcessRegistry",
    "ancestors_of",
    "list_procs",
    "new_record_id",
    "prepare_spawn",
    "probe",
    "registered_process",
    "run_registered",
    "scan_marks",
    "try_deregister_pid",
    "try_register",
    "verify_ours",
]


# --------------------------------------------------------------------------
# Live process inspection (ps only: macOS and Linux, no extra dependency)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    uid: int
    rss_mb: float
    stat: str
    cpu_s: float
    start: float  # epoch seconds

    @property
    def zombie(self) -> bool:
        return self.stat.startswith("Z")


def _parse_cpu(text: str) -> float:
    """``[[dd-]hh:]mm:ss[.ff]`` to seconds."""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    parts = [float(p) for p in text.split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return days * 86400 + secs


def _parse_ps_line(line: str) -> ProcInfo | None:
    f = line.split(None, 7)
    if len(f) < 8:
        return None
    try:
        start = time.mktime(time.strptime(f[7].strip(), "%a %b %d %H:%M:%S %Y"))
        return ProcInfo(
            pid=int(f[0]),
            ppid=int(f[1]),
            pgid=int(f[2]),
            uid=int(f[3]),
            rss_mb=int(f[4]) / 1024.0,
            stat=f[5],
            cpu_s=_parse_cpu(f[6]),
            start=start,
        )
    except (ValueError, OverflowError):
        return None


_PS_FIELDS = "pid=,ppid=,pgid=,uid=,rss=,stat=,time=,lstart="


def _ps(args: Sequence[str]) -> str:
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    try:
        res = subprocess.run(
            ["ps", *args], capture_output=True, text=True, timeout=15, env=env, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout


def probe(pid: int) -> ProcInfo | None:
    """The live process with this pid, or ``None`` if it does not exist."""
    if pid <= 0:
        return None
    for line in _ps(["-ww", "-p", str(pid), "-o", _PS_FIELDS]).splitlines():
        info = _parse_ps_line(line)
        if info and info.pid == pid:
            return None if info.zombie else info
    return None


def list_procs() -> dict[int, ProcInfo]:
    """Every visible process, in one ``ps`` call."""
    out: dict[int, ProcInfo] = {}
    for line in _ps(["-axww", "-o", _PS_FIELDS]).splitlines():
        info = _parse_ps_line(line)
        if info and not info.zombie:
            out[info.pid] = info
    return out


def ancestors_of(pid: int, procs: dict[int, ProcInfo] | None = None) -> set[int]:
    """``pid`` and every ancestor up to init. Used so a sweep never signals
    the process that is running the sweep, or the shell/agent that invoked it."""
    procs = procs if procs is not None else list_procs()
    seen: set[int] = set()
    cur = pid
    while cur > 1 and cur not in seen:
        seen.add(cur)
        info = procs.get(cur)
        if info is None:
            break
        cur = info.ppid
    seen.add(os.getpid())
    return seen


def scan_marks(*, scope: str | None = None, uid: int | None = None) -> dict[int, tuple[str, str]]:
    """pid -> (scope, record_id) for processes of OUR uid carrying the mark.

    Read-only. The environment is read once and reduced to the mark by regex;
    nothing else from it is kept. Used for escaped-child detection and as the
    panic fallback when the registry directory is damaged.
    """
    uid = os.getuid() if uid is None else uid
    found: dict[int, tuple[str, str]] = {}
    me = os.getpid()
    if Path("/proc/self/environ").exists():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                raw = (entry / "environ").read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            m = _MARK_RE.search(raw.replace("\0", " "))
            if m and int(entry.name) != me and (scope is None or m.group(1) == scope):
                found[int(entry.name)] = (m.group(1), m.group(2))
        return found
    # macOS / BSD: ``E`` appends the environment to the command column.
    own = {p for p, i in list_procs().items() if i.uid == uid}
    for line in _ps(["-axwwE", "-o", "pid=,command="]).splitlines():
        head, _, _rest = line.strip().partition(" ")
        if not head.isdigit() or int(head) not in own or int(head) == me:
            continue
        m = _MARK_RE.search(line)
        if m and (scope is None or m.group(1) == scope):
            found[int(head)] = (m.group(1), m.group(2))
    return found


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


def new_record_id() -> str:
    return uuid.uuid4().hex[:12]


def _scrub(argv: Sequence[str] | str, limit: int = 200) -> str:
    text = argv if isinstance(argv, str) else " ".join(str(a) for a in argv)
    return _SECRETISH.sub(lambda m: m.group(1) + "***", text)[:limit]


@dataclass
class ProcessRecord:
    id: str
    pid: int
    start_time: float
    pgid: int
    own_group: bool
    uid: int
    kind: str  # tool | agent | service | task | mcp | test
    purpose: str
    scope: str = DEFAULT_SCOPE
    principal: str = ""
    agent: str = ""
    extension: str = ""
    session: str = ""
    parent_pid: int = 0
    parent_start: float = 0.0
    #: Deliberately outlives its parent (a background task). Parent death is
    #: then not orphaning; only ``max_runtime_s`` can make it a problem.
    detached: bool = False
    max_runtime_s: float | None = None
    cpu_budget_s: float | None = None
    rss_budget_mb: float | None = None
    singleton_key: str = ""
    command: str = ""
    registered_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProcessRegistry:
    """One atomic JSON file per record under ``<state>/processes/records``."""

    def __init__(self, state_dir: Path | None = None, *, scope: str = DEFAULT_SCOPE):
        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()
        self.state_dir = Path(state_dir)
        self.scope = scope
        self.dir = self.state_dir / "processes" / "records"

    # -- write ------------------------------------------------------------

    def register(
        self,
        pid: int,
        *,
        kind: str,
        purpose: str,
        record_id: str | None = None,
        principal: str = "",
        agent: str = "",
        extension: str = "",
        session: str = "",
        parent_pid: int | None = None,
        detached: bool = False,
        max_runtime_s: float | None = None,
        cpu_budget_s: float | None = None,
        rss_budget_mb: float | None = None,
        singleton_key: str = "",
        command: Sequence[str] | str = "",
    ) -> ProcessRecord:
        info = probe(pid)
        if info is None:
            raise ProcessLookupError(f"pid {pid} is not running; nothing to register")
        session = session or os.environ.get("AXIOM_SESSION_ID", "")
        ppid = os.getpid() if parent_pid is None else parent_pid
        pinfo = probe(ppid)
        rec = ProcessRecord(
            id=record_id or new_record_id(),
            pid=pid,
            start_time=info.start,
            pgid=info.pgid,
            own_group=(info.pgid == pid and info.pgid != os.getpgrp()),
            uid=info.uid,
            kind=kind,
            purpose=purpose,
            scope=self.scope,
            principal=principal,
            agent=agent,
            extension=extension,
            session=session,
            parent_pid=ppid,
            parent_start=pinfo.start if pinfo else 0.0,
            detached=detached,
            max_runtime_s=max_runtime_s,
            cpu_budget_s=cpu_budget_s,
            rss_budget_mb=rss_budget_mb,
            singleton_key=singleton_key,
            command=_scrub(command),
        )
        self._write(rec)
        return rec

    def _write(self, rec: ProcessRecord) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        final = self.dir / f"{rec.id}.json"
        tmp = self.dir / f".{rec.id}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(rec.to_dict(), sort_keys=True), encoding="utf-8")
        os.replace(tmp, final)

    def deregister(self, record_id: str) -> bool:
        try:
            (self.dir / f"{record_id}.json").unlink()
            return True
        except OSError:
            return False

    # -- read -------------------------------------------------------------

    def load(self) -> tuple[list[ProcessRecord], list[str]]:
        """(records, damaged_file_names). A damaged file never hides the rest."""
        recs: list[ProcessRecord] = []
        damaged: list[str] = []
        try:
            names = sorted(p for p in self.dir.iterdir() if p.suffix == ".json")
        except FileNotFoundError:
            return [], []
        except OSError:
            return [], ["<registry directory unreadable>"]
        known = set(ProcessRecord.__dataclass_fields__)
        for path in names:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                recs.append(ProcessRecord(**{k: v for k, v in data.items() if k in known}))
            except (OSError, ValueError, TypeError):
                damaged.append(path.name)
        return recs, damaged

    def records(self) -> list[ProcessRecord]:
        return self.load()[0]

    def get(self, id_or_prefix: str) -> ProcessRecord | None:
        matches = [r for r in self.records() if r.id.startswith(id_or_prefix)]
        return matches[0] if len(matches) == 1 else None

    def reap(self) -> list[str]:
        """Drop records whose process has exited (clean-exit deregistration
        for processes that outlived the context that started them)."""
        gone = []
        for rec in self.records():
            if probe(rec.pid) is None:
                self.deregister(rec.id)
                gone.append(rec.id)
        return gone


# --------------------------------------------------------------------------
# Identity check: the gate in front of every signal
# --------------------------------------------------------------------------


def verify_ours(
    rec: ProcessRecord, info: ProcInfo | None = None
) -> tuple[bool, str, ProcInfo | None]:
    """Is the live process at ``rec.pid`` really the one this record names?

    Returns ``(ok, reason, live)``. All of these must hold, and the failure
    reason says which did not. Never signal on anything but ``ok``.
    """
    live = info if info is not None else probe(rec.pid)
    if rec.pid <= 1 or rec.pid == os.getpid():
        return False, "refusing: protected pid", live
    if live is None:
        return False, "process is not running", None
    if live.uid != os.getuid():
        return False, "process belongs to another user", live
    if abs(live.start - rec.start_time) > START_TOLERANCE_S:
        return (
            False,
            f"pid reused: record start {rec.start_time:.0f} != live start {live.start:.0f}",
            live,
        )
    return True, "record matches live pid and start time", live


# --------------------------------------------------------------------------
# Registration helpers for spawn points
# --------------------------------------------------------------------------


def prepare_spawn(
    env: dict[str, str] | None = None,
    *,
    scope: str = DEFAULT_SCOPE,
    record_id: str | None = None,
) -> tuple[str, dict[str, str]]:
    """(record_id, env) to hand to ``Popen`` so the child carries its mark."""
    rid = record_id or new_record_id()
    out = dict(os.environ if env is None else env)
    out[MARK_ENV] = f"{scope}:{rid}"
    return rid, out


def try_register(
    pid: int, *, registry: ProcessRegistry | None = None, **kwargs: Any
) -> ProcessRecord | None:
    """Register, never raising: a registry fault must not break a spawn."""
    try:
        return (registry or ProcessRegistry()).register(pid, **kwargs)
    except Exception:  # noqa: BLE001
        return None


def try_deregister_pid(pid: int | None, *, registry: ProcessRegistry | None = None) -> None:
    """Drop the record(s) for ``pid``, never raising. Only call this once the
    process is known to have ended (or has just been stopped)."""
    if not pid:
        return
    try:
        reg = registry or ProcessRegistry()
        for rec in reg.records():
            if rec.pid == pid:
                reg.deregister(rec.id)
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def registered_process(
    proc: Any,
    *,
    registry: ProcessRegistry | None = None,
    **kwargs: Any,
) -> Iterator[ProcessRecord | None]:
    """Register ``proc`` (a ``Popen`` or a pid) for the duration of the block.

    On exit the record is dropped if the process has exited; a process that
    is still running (a deliberately detached task) keeps its record, which
    ``ProcessRegistry.reap`` removes once it is gone.
    """
    reg = registry or ProcessRegistry()
    pid = proc if isinstance(proc, int) else proc.pid
    kwargs.setdefault("command", getattr(proc, "args", "") or "")
    rec = try_register(pid, registry=reg, **kwargs)
    try:
        yield rec
    finally:
        if rec is not None:
            if not isinstance(proc, int) and hasattr(proc, "poll"):
                proc.poll()  # reap a zombie so probe() sees the exit
            if probe(rec.pid) is None:
                reg.deregister(rec.id)


def run_registered(
    cmd: Sequence[str],
    *,
    timeout: float | None = None,
    input: bytes | str | None = None,  # noqa: A002 - mirrors subprocess.run
    capture_output: bool = False,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    new_session: bool = False,
    registry: ProcessRegistry | None = None,
    kind: str = "tool",
    purpose: str = "",
    **reg: Any,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` that records the child while it runs.

    Same contract as ``subprocess.run`` for the arguments it accepts (a
    timeout kills the child and raises ``TimeoutExpired``) with three
    additions: the child carries the harness mark, it is registered for its
    lifetime with a max runtime of ``timeout`` plus slack (so a child that
    survives its own timeout shows up as an overrun), and with
    ``new_session=True`` the timeout kills the whole process group, so a
    worker's own children do not outlive it.
    """
    reg_ = registry or ProcessRegistry()
    rid, child_env = prepare_spawn(env, scope=reg_.scope)
    proc = subprocess.Popen(
        list(cmd),
        env=child_env,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        start_new_session=new_session,
        text=isinstance(input, str),
    )
    if timeout is not None:
        reg.setdefault("max_runtime_s", float(timeout) + 30.0)
    with registered_process(
        proc,
        registry=reg_,
        record_id=rid,
        kind=kind,
        purpose=purpose or "subprocess",
        command=list(cmd),
        **reg,
    ):
        try:
            out, err = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc, new_session)
            proc.communicate()
            raise
        except BaseException:
            _kill_tree(proc, new_session)
            raise
    return subprocess.CompletedProcess(list(cmd), proc.returncode, out, err)


def _kill_tree(proc: subprocess.Popen, group: bool) -> None:
    """Kill a child WE just started (we hold its Popen, so the pid is ours)."""
    import signal

    try:
        if group:
            os.killpg(proc.pid, signal.SIGKILL)  # new_session made pid the group id
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass
