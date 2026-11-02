# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Generalized public-tunnel primitive (any inbound connector reuses it)."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.connector.tunnel import (
    CloudflaredProvider,
    TunnelHandle,
    TunnelUnavailable,
    open_tunnel,
)


class _FakeProc:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.terminated = False

    def terminate(self):
        self.terminated = True


def _spawn_with(lines):
    proc = _FakeProc(lines)
    return lambda cmd: proc, proc


# --- happy path: URL parsed from output ----------------------------------- #
def test_open_parses_public_url():
    spawn, proc = _spawn_with(
        [
            "INF Requesting new quick Tunnel...",
            "INF |  https://warned-nissan-hugh.trycloudflare.com  |",
            "INF Registered tunnel connection",
        ]
    )
    h = open_tunnel(8799, provider=CloudflaredProvider(spawn=spawn))
    assert h.public_url == "https://warned-nissan-hugh.trycloudflare.com"
    assert h.provider == "cloudflared"


def test_webhook_url_joins_path():
    spawn, _ = _spawn_with(["https://abc.trycloudflare.com"])
    h = open_tunnel(8799, provider=CloudflaredProvider(spawn=spawn))
    assert h.webhook_url("/herald/inbound/slack") == (
        "https://abc.trycloudflare.com/herald/inbound/slack"
    )
    assert h.webhook_url("herald/inbound/slack").endswith("/herald/inbound/slack")


def test_stop_calls_terminate():
    spawn, proc = _spawn_with(["https://abc.trycloudflare.com"])
    h = open_tunnel(8799, provider=CloudflaredProvider(spawn=spawn))
    h.stop()
    assert proc.terminated is True


def test_stop_is_idempotent_and_swallows_errors():
    def boom():
        raise RuntimeError("already gone")

    h = TunnelHandle(public_url="https://x.trycloudflare.com", _stop=boom)
    h.stop()  # must not raise
    h.stop()


# --- failure modes: clear TunnelUnavailable ------------------------------- #
def test_no_url_in_output_raises_and_terminates():
    spawn, proc = _spawn_with(["INF starting", "INF no url here", "INF done"])
    with pytest.raises(TunnelUnavailable, match="no public URL"):
        open_tunnel(8799, provider=CloudflaredProvider(spawn=spawn), timeout=5)
    assert proc.terminated is True


def test_missing_binary_message_is_actionable(monkeypatch):
    import axiom.extensions.builtins.connector.tunnel as t

    monkeypatch.setattr(t.shutil, "which", lambda _: None)
    prov = CloudflaredProvider()  # default real spawn → availability gate trips
    assert prov.available() is False
    with pytest.raises(TunnelUnavailable, match="brew install cloudflared"):
        prov.open(8799)
