# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`SSHRunner` — `RemoteRunner` impl backed by the `ssh` CLI (issue #205, slice 2).

Talks to a remote host via `subprocess.run(["ssh", host, ...])`. Path
operations are intentionally absent — go through `run()` with a
host-native command (`ls`, `test -f`, etc.) instead. The only file-style
op is `write_file()`, which exists because the scheduler needs to lay
down small text artifacts (cron files, plist files) and a `cat`-redirect
over ssh is the smallest portable hammer.

`BatchMode=yes` blocks interactive password prompts — important for
tests + agent runs. If the host requires a password, the caller arranges
key-based auth beforehand (production reality).
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

from .protocols import CompletedRun


@dataclass
class SSHRunner:
    """Concrete `RemoteRunner` for ssh-reachable hosts."""

    host: str
    ssh_args: tuple[str, ...] = ("-o", "BatchMode=yes")

    def _argv_prefix(self) -> list[str]:
        return ["ssh", *self.ssh_args, self.host]

    def run(
        self,
        command: list[str] | str,
        *,
        input: str | None = None,
    ) -> CompletedRun:
        if isinstance(command, str):
            argv = [*self._argv_prefix(), command]
        else:
            argv = [*self._argv_prefix(), *command]

        result = subprocess.run(
            argv,
            input=input,
            capture_output=True,
            text=True,
            check=False,
        )
        return CompletedRun(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            returncode=result.returncode,
        )

    def write_file(self, remote_path: str, content: str) -> None:
        """Lay down `content` at `remote_path` on the host. Quotes the
        path for shell safety; pipes content via stdin."""
        quoted = shlex.quote(remote_path)
        argv = [*self._argv_prefix(), f"cat > {quoted}"]
        result = subprocess.run(
            argv,
            input=content,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"SSHRunner.write_file({remote_path!r}) on host "
                f"{self.host!r} failed: rc={result.returncode} "
                f"stderr={(result.stderr or '').strip()!r}"
            )
