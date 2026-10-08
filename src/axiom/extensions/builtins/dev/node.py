# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Run a node from a checkout, the way a deployed node is run, with one command.

Everything here was once a step somebody typed by hand to get a local node up:
the source roots on ``PYTHONPATH``, an accounts file the gate reads, a first
account, the front door pointing at the app, the sign-in their vault already
held. Each one missed produced a node that started and then did not work —
an unstyled index at ``/``, a sign-in that refused every password — and none of
them said why.

State for one checkout lives under ``<state>/dev/<lane>/``: the accounts file,
the log, and a record of the running process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: What ``/`` redirects to, in order of preference: a consumer's app if one is
#: mounted, else the platform's own. The node picks the first it actually serves
#: (compose ``_choose_surface``); assuming one path sent a plain node to a 404.
SURFACES = "/app/,/receipts/"


@dataclass(frozen=True)
class Plan:
    lane: str
    root: Path
    port: int
    home: Path
    command: list[str]
    env: dict[str, str] = field(repr=False)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def log(self) -> Path:
        return self.home / "node.log"

    @property
    def record(self) -> Path:
        return self.home / "node.json"

    @property
    def accounts(self) -> Path:
        return self.home / "users.json"


def home_for(state_dir: Path, lane: str) -> Path:
    return Path(state_dir) / "dev" / lane


def source_roots(root: Path) -> list[Path]:
    """The checkout's import roots, as its own ``pyproject.toml`` declares them.

    Read from the file rather than assumed, so a repo whose package is not
    under ``src/`` still runs its own code. Falls back to ``src`` when the
    project declares nothing.
    """
    declared: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        declared = list(
            data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("pythonpath", [])
        )
    roots = [root / p for p in (declared or ["src"])]
    return [p for p in roots if p.is_dir()]


def build_plan(
    *,
    lane: str,
    root: Path,
    port: int,
    state_dir: Path,
    extra_roots: list[Path] | None = None,
    sign_in_env: dict[str, str] | None = None,
    base_env: dict[str, str] | None = None,
    python: str | None = None,
) -> Plan:
    home = home_for(state_dir, lane)
    roots = [*(extra_roots or []), *source_roots(root)]
    env = dict(os.environ if base_env is None else base_env)
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [*(str(p) for p in roots), *([inherited] if inherited else [])]
    )
    env["AXIOM_GATE_USERS_FILE"] = str(home / "users.json")
    env["AXIOM_DEFAULT_SURFACE"] = SURFACES
    # Diagnoses from earlier commands are for the person at the terminal, not
    # for a node's log, where they read as this node's own failures.
    env["AXI_DIAGNOSES_QUIET"] = "1"
    env.update(sign_in_env or {})
    command = [
        python or sys.executable,
        "-m",
        "axiom.axiom_cli",
        "serve",
        "--profile",
        "server",
        "--insecure",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    return Plan(lane=lane, root=root, port=port, home=home, command=command, env=env)


# -- the first account -------------------------------------------------------


def secret_name(lane: str) -> str:
    return f"dev-gate-{lane}"


def ensure_owner(plan: Plan, email: str, vault: Any) -> dict[str, Any]:
    """Make sure ``email`` can sign in, holding the password in the vault.

    An existing account is left alone. A new one gets a generated password
    that goes straight into the vault and is never returned, so it cannot land
    in a terminal, a log or a transcript; the person reads it from the vault
    when they need it.
    """
    from axiom.extensions.builtins.webgate.skills._accounts import generate_password
    from axiom.webauth import get_password_hash, load_user_records, upsert_user_record

    plan.home.mkdir(parents=True, exist_ok=True)
    existing = []
    if plan.accounts.exists():
        existing = [r for r in load_user_records(plan.accounts) if r.get("email") == email]
    name = secret_name(plan.lane)
    if existing and vault.exists(name):
        return {"email": email, "created": False, "secret": name}
    password = generate_password()
    upsert_user_record(
        plan.accounts,
        email=email,
        password_hash=get_password_hash(password),
        roles=["owner"],
        user_id=email,
        overwrite=True,
    )
    os.chmod(plan.accounts, 0o600)
    vault.set(name, password.encode("utf-8"), notes=f"password for {email} on dev node {plan.lane}")
    return {"email": email, "created": True, "secret": name}


# -- the process -------------------------------------------------------------


def read_record(plan_or_home: Plan | Path) -> dict[str, Any] | None:
    path = plan_or_home.record if isinstance(plan_or_home, Plan) else plan_or_home / "node.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def alive(pid: int) -> bool:
    # A node this process started and that has since exited is a zombie until
    # reaped, and signal 0 still succeeds on a zombie, so reap first. A pid
    # that is not our child raises ChildProcessError, which is the usual case:
    # `dev down` runs in a different process from the `dev up` that started it.
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def answers(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start(plan: Plan) -> int:
    plan.home.mkdir(parents=True, exist_ok=True)
    with open(plan.log, "ab") as log:
        proc = subprocess.Popen(
            plan.command,
            cwd=plan.root,
            env=plan.env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    plan.record.write_text(
        json.dumps(
            {
                "pid": proc.pid,
                "port": plan.port,
                "url": plan.url,
                "root": str(plan.root),
                "log": str(plan.log),
                "started_at": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return proc.pid


def tail(path: Path, lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def served_url(url: str, timeout: float = 5.0) -> str:
    """Where the node's front door actually lands, after its redirect."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.geturl()
    except (urllib.error.URLError, OSError, ValueError):
        return url


def remember_url(home: Path, url: str) -> None:
    record = read_record(home)
    if record is not None:
        record["url"] = url
        (home / "node.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def wait_ready(plan: Plan, pid: int, timeout: float = 90.0) -> tuple[bool, str]:
    """Until the app answers, the process dies, or time runs out.

    "The process started" is not the claim: a node can bind its port and still
    fail every request. The app answering 200 is.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if answers(plan.url):
            return True, ""
        if not alive(pid):
            return False, "the node exited while starting"
        time.sleep(0.5)
    return False, f"the node did not answer {plan.url} within {int(timeout)}s"


def _signal_group(pid: int, sig: int) -> None:
    """Signal the node and anything it started; gone already is fine."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError:
        pass


def stop(home: Path, timeout: float = 15.0) -> dict[str, Any]:
    record = read_record(home)
    if record is None:
        return {"stopped": False, "reason": "no node is recorded for this checkout"}
    pid = int(record["pid"])
    if not alive(pid):
        (home / "node.json").unlink(missing_ok=True)
        return {"stopped": False, "reason": f"process {pid} had already exited", "pid": pid}
    _signal_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if alive(pid):
        _signal_group(pid, signal.SIGKILL)
    (home / "node.json").unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


__all__ = [
    "SURFACES",
    "Plan",
    "alive",
    "answers",
    "build_plan",
    "ensure_owner",
    "home_for",
    "read_record",
    "remember_url",
    "secret_name",
    "served_url",
    "source_roots",
    "start",
    "stop",
    "tail",
    "wait_ready",
]
