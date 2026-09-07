# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`LocalRunner` — RemoteRunner impl that runs commands in the local
process via `subprocess.run` (issue #205, slice 7).

Sibling of `SSHRunner`. Used when `--host localhost` is passed —
typically because the schedule target is the user's own machine
(LaunchdBackend against `~/Library/LaunchAgents/` on this Mac, or a
self-systemd-timer in dev). Saves a needless ssh-to-self round trip,
and removes the dependency on sshd being configured locally.

Defensive on the host attribute: anything that isn't `localhost`/`local`
is rejected. Without that guard, `LocalRunner(host="<remote>")` would
silently run remote-targeted install commands on the local box —
caller should use SSHRunner for non-local hosts.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .protocols import CompletedRun

_ALLOWED_HOSTS = ("localhost", "local")


@dataclass
class LocalRunner:
    host: str = "localhost"

    def __post_init__(self) -> None:
        if self.host not in _ALLOWED_HOSTS:
            raise ValueError(
                f"LocalRunner host must be one of {_ALLOWED_HOSTS}, "
                f"got {self.host!r}. For non-local hosts, use SSHRunner."
            )

    def run(
        self,
        command: list[str] | str,
        *,
        input: str | None = None,
    ) -> CompletedRun:
        kwargs: dict = {
            "capture_output": True,
            "text": True,
            "check": False,
        }
        if input is not None:
            kwargs["input"] = input
        if isinstance(command, str):
            kwargs["shell"] = True
            result = subprocess.run(command, **kwargs)
        else:
            result = subprocess.run(command, **kwargs)
        return CompletedRun(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            returncode=result.returncode,
        )

    def write_file(self, remote_path: str, content: str) -> None:
        path = Path(remote_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        except OSError as exc:
            raise RuntimeError(
                f"write_file({remote_path!r}) failed: {exc}"
            ) from exc
