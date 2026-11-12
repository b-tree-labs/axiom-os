# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The checkout's frontend dev server, started on the lane's front port.

Same discipline as the node: a process group of its own, a pid record under
the lane's dev home, and only that record's pid is ever signalled.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from . import config, node


def record_path(home: Path) -> Path:
    return Path(home) / "web.json"


def read_record(home: Path) -> dict[str, Any] | None:
    try:
        return json.loads(record_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def url_for(front: int, path: str = "/") -> str:
    return f"http://127.0.0.1:{front}{path}"


def start(
    web: config.Web, *, root: Path, home: Path, front: int, node_url: str, base_env: dict[str, str]
) -> int:
    home.mkdir(parents=True, exist_ok=True)
    env = dict(base_env)
    env.update({k: config.fill(v, port=front, node_url=node_url) for k, v in web.env.items()})
    cmd = shlex.split(config.fill(web.command, port=front, node_url=node_url))
    cwd = (Path(root) / web.cwd).resolve()
    log = home / "web.log"
    with open(log, "ab") as fh:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    record_path(home).write_text(
        json.dumps(
            {
                "pid": proc.pid,
                "port": front,
                "url": url_for(front, web.ready_path),
                "log": str(log),
                "started_at": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return proc.pid


def wait_ready(url: str, pid: int, timeout: float = 60.0) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if node.answers(url):
            return True, ""
        if not node.alive(pid):
            return False, "the web dev server exited while starting"
        time.sleep(0.4)
    return False, f"the web dev server did not answer {url} within {int(timeout)}s"


def stop(home: Path, timeout: float = 10.0) -> dict[str, Any]:
    record = read_record(home)
    if record is None:
        return {"stopped": False, "reason": "no web dev server is recorded"}
    pid = int(record["pid"])
    if node.alive(pid):
        node._signal_group(pid, 15)
        deadline = time.monotonic() + timeout
        while node.alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if node.alive(pid):
            node._signal_group(pid, 9)
        out = {"stopped": True, "pid": pid}
    else:
        out = {"stopped": False, "reason": f"process {pid} had already exited", "pid": pid}
    record_path(home).unlink(missing_ok=True)
    return out


__all__ = ["read_record", "start", "stop", "url_for", "wait_ready"]
