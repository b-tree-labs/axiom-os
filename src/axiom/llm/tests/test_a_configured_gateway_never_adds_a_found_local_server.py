# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A gateway with configured providers never adds a model server it found on localhost.

Seen live on a development lane configured with one provider: when that provider failed (HTTP 402),
the answer came from an unrelated model server that happened to be listening on localhost:8080,
which the gateway had registered as a fallback at start-up. Whatever holds that port on a shared
machine is nobody's choice for this lane. Discovery is for a machine with nothing configured; with
providers configured, a provider's failure is the answer's error, said plainly.
"""

from __future__ import annotations

import socket

from axiom.llm import gateway as gw


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _a_server_on_8080(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _Conn())
    monkeypatch.setattr(gw, "_listed_model_id", lambda endpoint: "someone-elses-model")


def test_a_configured_gateway_adds_no_local_server_and_one_with_nothing_configured_does(tmp_path, monkeypatch):
    _a_server_on_8080(monkeypatch)
    configured = tmp_path / "configured"
    configured.mkdir()
    (configured / "llm-providers.toml").write_text(
        '[gateway]\n[[gateway.providers]]\nname = "lane-provider"\nuid = "u-1"\n'
        'endpoint = "https://llm.example.org/v1"\nmodel = "m"\npriority = 1\n'
        'use_for = ["chat", "fallback"]\n',
        encoding="utf-8",
    )
    assert [p.name for p in gw.Gateway(config_dir=configured).providers] == ["lane-provider"]

    empty = tmp_path / "empty"
    empty.mkdir()
    assert [p.name for p in gw.Gateway(config_dir=empty).providers] == ["axiom-local"]
