# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The restore-failure drill harness: one interface over state snapshots, vault backups and pg dumps.

A drill damages a backup (or the place it lives, or the thing being restored INTO), runs a real
restore through the real CLI or the emergency script, and then asks what a person would want to know:

* did it crash, hang, leak a secret, or leave a torn / half-written / silently wrong result?  -> FAIL
* otherwise it must land in exactly one OUTCOME class:

  ``restored``                  the newest backup was restored, exactly
  ``restored-from-fallback``    an older good backup was restored, and the output says which newer
                                ones were passed over and why (or that a second copy was used)
  ``partial-restore-reported``  some items restored and some not, and the output names both sets
  ``refused-safely``            nothing changed; a named reason and a next step were printed

The judge never trusts the product's own account: it compares the target's actual content before
and after with the content of each backup generation.
"""

from __future__ import annotations

import base64
import contextlib
import io
import logging
import os
import re
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.infra import backup_core as bc

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "axi-restore.py"
OUTCOMES = ("restored", "restored-from-fallback", "partial-restore-reported", "refused-safely")


class DrillFailure(AssertionError):
    """The result was a crash, a hang, a leak, a torn state, or silent wrong data."""


@dataclass
class Result:
    rc: int | None
    out: str
    crashed: str | None = None
    killed: bool = False  # a simulated power cut / deliberate kill: there is no report to read
    hung: bool = False


@dataclass
class Judged:
    cls: str
    result: Result
    after: dict[str, str]
    matched_gen: int | None = None
    notes: list[str] = field(default_factory=list)


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            self.lines.append(record.getMessage())


def call(fn, *, timeout: float = 120.0, capture: bool = True) -> Result:
    """Run *fn* (an in-process CLI call) capturing everything a person would see, with a watchdog."""
    buf, handler, box = io.StringIO(), _ListHandler(), {}
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    def run() -> None:
        # (two overlapping redirects would tangle sys.stdout; a concurrency drill passes capture=False)
        cm = (contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf)) if capture else ()
        with contextlib.ExitStack() as stack:
            for c in cm:
                stack.enter_context(c)
            try:
                box["rc"] = fn()
            except bc.SimulatedCrash:
                box["killed"] = True
            except SystemExit as e:
                box["rc"] = e.code if isinstance(e.code, int) else 1
            except Exception as e:  # noqa: BLE001 -- this IS the crash detector
                box["crashed"] = f"{type(e).__name__}: {e}"
                buf.write(traceback.format_exc())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    root.removeHandler(handler)
    root.setLevel(old_level)
    text = buf.getvalue() + "\n".join(handler.lines)
    return Result(rc=box.get("rc"), out=text, crashed=box.get("crashed"), killed=bool(box.get("killed")),
                  hung=t.is_alive())


def run_script(*args: str, cwd: Path, shadow: Path | None = None, driver: Path | None = None,
               driver_args: tuple[str, ...] = (), stdin: str | None = None, timeout: float = 180.0) -> Result:
    """Run the emergency script (or a fault driver around it) the way a broken install forces:
    ``python -I`` with a shadow ``axiom`` package that raises on import, nothing from Axiom importable."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    if shadow is not None:
        env["PYTHONPATH"] = str(shadow)  # ignored by -I; present to prove -I does the isolating
    cmd = [sys.executable, "-I"]
    cmd += [str(driver), *driver_args, str(SCRIPT), *args] if driver else [str(SCRIPT), *args]
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return Result(rc=None, out=str(e.stdout or "") + str(e.stderr or ""), hung=True)
    return Result(rc=p.returncode, out=p.stdout + p.stderr, killed=p.returncode in (9, -9))


DRIVER = '''\
# Fault driver for the emergency script: runs it in-process with os.replace under control.
import os, runpy, sys
mode, n, pat, script = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
if mode == "fsize":                          # a disk that holds at most n bytes per file
    import resource, signal
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (n, n))
real = os.replace
count = {"n": 0}
def replace(src, dst, *a, **k):
    if ".restore-" in str(src):
        count["n"] += 1
        if mode == "crash" and count["n"] == n:
            os._exit(9)                      # power cut just before the rename
        if mode == "drop" and str(dst).endswith(pat):
            os.unlink(src)                   # the write never lands
            return None
    return real(src, dst, *a, **k)
os.replace = replace
sys.argv = [script, *sys.argv[5:]]
runpy.run_path(script, run_name="__main__")
'''


def secret_variants(s: str) -> list[str]:
    return [s, base64.b64encode(s.encode()).decode(), base64.b64encode(s.encode()).decode().rstrip("=")]


HAS_NEXT = re.compile(r"(next:|\btry\b|--force|--from|--snapshots|--code-file|python3 -I|\baxi\b|another copy|second copy|"
                      r"\bvault\b.*\b(ls|verify|restore|key)|state (ls|verify|restore))", re.I)


class Harness:
    kind = ""
    hows: frozenset[str] = frozenset()

    def __init__(self, sandbox: Path, state_dir: Path) -> None:
        self.sandbox, self.state = sandbox, state_dir
        self.ids: list[str] = []
        self.gen_digests: dict[int, dict[str, str]] = {}
        self.secrets: list[str] = []
        self.second: Path | None = None
        self.capture = True
        self.root: Path = sandbox / "backups"

    # -- to implement --------------------------------------------------------------------------
    def make_generations(self, n: int = 3, **kw: Any) -> None: ...
    def break_target(self) -> None: ...
    def digest(self) -> dict[str, str]: ...
    def damage(self, bid: str, how: str) -> None: ...
    def restore_latest(self, **kw: Any) -> Result: ...
    def restore_id(self, bid: str, **kw: Any) -> Result: ...
    def script_restore_latest(self, **kw: Any) -> tuple[Result, dict[str, str]]: ...
    def verify(self, **kw: Any) -> Result: ...
    def debris(self) -> list[str]: return []
    def newest(self) -> str: return self.ids[-1]

    # -- shared --------------------------------------------------------------------------------
    def matches(self, after: dict[str, str], gen: int) -> bool:
        return all(after.get(k) == v for k, v in self.gen_digests[gen].items())

    def allowed_tokens(self, key: str, before: dict[str, str]) -> set[str | None]:
        toks: set[str | None] = {before.get(key)}
        toks |= {d.get(key) for d in self.gen_digests.values()}
        return toks


def judge(h: Harness, res: Result, before: dict[str, str], *, after: dict[str, str] | None = None,
          must_mention: tuple[str, ...] = (), allow_debris: bool = False,
          fallback_ok_without_note: bool = False, explicit: bool = False) -> Judged:
    """Classify an outcome, or raise :class:`DrillFailure` for anything that is not one of the four."""
    out = res.out
    if res.hung:
        raise DrillFailure(f"HANG: no result within the watchdog\n{out[-600:]}")
    if res.crashed:
        raise DrillFailure(f"CRASH: {res.crashed}")
    if "Traceback (most recent call last)" in out:
        raise DrillFailure(f"CRASH (traceback in output):\n{out[-800:]}")
    for s in h.secrets:
        for v in secret_variants(s):
            if v and v in out:
                raise DrillFailure(f"SECRET IN OUTPUT: a canary/secret of length {len(s)} appeared in the output")
    after = h.digest() if after is None else after
    keys = set(after) | set(before) | {k for d in h.gen_digests.values() for k in d}
    for k in sorted(keys):
        if after.get(k) not in h.allowed_tokens(k, before):
            raise DrillFailure(f"TORN OR SILENTLY WRONG: {k!r} is neither what it was nor any backup's version")
    if not allow_debris and not res.killed:
        d = h.debris()
        if d:
            raise DrillFailure(f"HALF-WRITTEN DEBRIS left behind: {d[:3]}")
    for m in must_mention:
        if m not in out:
            raise DrillFailure(f"the output does not mention {m!r}:\n{out[-800:]}")
    if res.killed:
        return Judged("killed", res, after)

    newest = len(h.ids)
    matched = next((g for g in range(newest, 0, -1) if h.matches(after, g)), None)
    if res.rc == 0:
        if matched is None:
            if after != before and "PARTIAL RESTORE" in out:  # exit 0, but it said what it left alone
                return Judged("partial-restore-reported", res, after, None)
            if after == before:
                return Judged("restored", res, after, None, ["nothing to change"])
            raise DrillFailure(f"SILENT WRONG DATA: exit 0 but the result matches no backup\n{out[-600:]}")
        second = "USED THE SECOND COPY" in out
        if (matched == newest and not second) or explicit:  # (naming an older backup is not a fallback)
            return Judged("restored", res, after, matched)
        fell = bool(re.search(r"(FELL BACK|skipped)", out)) and bool(h.ids) and h.ids[-1] in out
        if not (second or fell) and not fallback_ok_without_note:
            raise DrillFailure(f"SILENT FALLBACK: restored generation {matched} of {newest} without saying so\n{out[-600:]}")
        return Judged("restored-from-fallback", res, after, matched)
    # non-zero exit
    if after == before:
        if not (HAS_NEXT.search(out) and len(out.strip()) > 30):
            raise DrillFailure(f"REFUSED WITHOUT A REASON OR A NEXT STEP:\n{out[-600:]}")
        return Judged("refused-safely", res, after)
    if not re.search(r"(PARTIAL RESTORE|NOT restored|not restored|re-verification failed)", out):
        raise DrillFailure(f"PARTIAL RESULT NOT REPORTED: the target changed but the output does not say what "
                           f"was and was not restored\n{out[-600:]}")
    return Judged("partial-restore-reported", res, after, matched)
