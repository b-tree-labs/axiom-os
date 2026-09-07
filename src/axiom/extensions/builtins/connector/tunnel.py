# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Public-tunnel primitive — any inbound connector's path to the internet.

A vendor that receives webhooks (Slack Events, Twilio, Microsoft Graph,
GitHub, …) needs a public URL pointing at the local gateway. Rather than
make each operator install + run + copy a tunnel by hand, this is a
generalized, provider-based primitive every inbound connector reuses:

    handle = open_tunnel(local_port=8799)
    handle.public_url                     # https://<...>.trycloudflare.com
    handle.webhook_url("/herald/inbound/slack")
    handle.stop()

Default provider is ``cloudflared`` (quick tunnel, no account). The
``TunnelProvider`` Protocol lets ngrok/localtunnel/named-tunnel back-ends
drop in without touching callers. Headless/missing-binary cases raise a
clear :class:`TunnelUnavailable` with the one install command, never a
stack trace.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_CF_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_DEFAULT_TIMEOUT = 30.0


class TunnelUnavailable(RuntimeError):
    """Raised when no tunnel can be established (missing binary / timeout)."""


@dataclass
class TunnelHandle:
    """A live tunnel. ``stop()`` tears it down; safe to call more than once."""

    public_url: str
    _stop: Callable[[], None]
    provider: str = "cloudflared"

    def webhook_url(self, path: str) -> str:
        return f"{self.public_url.rstrip('/')}/{path.lstrip('/')}"

    def stop(self) -> None:
        try:
            self._stop()
        except Exception:
            pass


@runtime_checkable
class TunnelProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def open(self, local_port: int, *, timeout: float = ...) -> TunnelHandle: ...


class _Proc(Protocol):
    stdout: Iterable[str] | None

    def terminate(self) -> None: ...


class CloudflaredProvider:
    """Cloudflared quick-tunnel provider. ``spawn`` is injectable for tests."""

    name = "cloudflared"

    def __init__(self, spawn: Callable[[list[str]], _Proc] | None = None) -> None:
        self._spawn = spawn or _default_spawn

    def available(self) -> bool:
        return shutil.which("cloudflared") is not None

    def open(self, local_port: int, *, timeout: float = _DEFAULT_TIMEOUT) -> TunnelHandle:
        if self._spawn is _default_spawn and not self.available():
            raise TunnelUnavailable(
                "cloudflared not found — install it once:\n    brew install cloudflared\n"
                "(macOS) / see https://github.com/cloudflare/cloudflared (Linux/Windows)"
            )
        proc = self._spawn(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{local_port}"]
        )
        url = _read_url(proc, _CF_URL_RE, timeout=timeout)
        if not url:
            try:
                proc.terminate()
            except Exception:
                pass
            raise TunnelUnavailable(
                f"cloudflared started but no public URL appeared within {timeout:g}s"
            )
        return TunnelHandle(public_url=url, _stop=proc.terminate, provider=self.name)


def _default_spawn(cmd: list[str]) -> _Proc:
    import subprocess

    return subprocess.Popen(  # noqa: S603 — fixed argv, local tunnel
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


def _read_url(proc: _Proc, pattern: re.Pattern[str], *, timeout: float) -> str | None:
    """Scan the process output for the first matching URL (bounded by timeout)."""
    import time

    deadline = time.monotonic() + timeout
    if proc.stdout is None:
        return None
    for line in proc.stdout:
        m = pattern.search(line)
        if m:
            return m.group(0)
        if time.monotonic() > deadline:
            break
    return None


def open_tunnel(
    local_port: int,
    *,
    provider: TunnelProvider | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> TunnelHandle:
    """Open a public tunnel to ``local_port``. Default provider: cloudflared."""
    prov = provider or CloudflaredProvider()
    return prov.open(local_port, timeout=timeout)


__all__ = [
    "TunnelHandle",
    "TunnelProvider",
    "CloudflaredProvider",
    "TunnelUnavailable",
    "open_tunnel",
]
