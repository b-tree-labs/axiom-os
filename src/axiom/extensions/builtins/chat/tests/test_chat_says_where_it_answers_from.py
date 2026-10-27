# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Chat says where it answers from: once when it starts, and under every reply.

A colleague's bound laptop printed ``↳ REMOTE? <host> · RAG: none`` and nothing that said the
answers came from a site at all. Now one banner line names the mode (BOUND, LOCAL or LOCAL TEST
MODE), the footer names the data source and the document source in words, and a machine running
on invented sample data is marked on every reply.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import where
from axiom.extensions.builtins.data_platform import read_target
from axiom.infra.orchestrator.session import Session
from tests.test_gateway import _gateway_with_providers, _make_provider

FACE = "https://node.example.org"


class _Settings:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    for name in ("DATABASE_URL", "DP1_RAG_DSN", "AXIOM_DB_URL", where.SAMPLE_DATA_ENV):
        monkeypatch.delenv(name, raising=False)
    values: dict = {}
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore", lambda: _Settings(values)
    )
    monkeypatch.setattr(read_target, "holds_medallion", lambda: bool(_env_dsn()))
    return state


def _env_dsn() -> str:
    import os

    return os.environ.get("DATABASE_URL", "")


def _bind(state) -> None:
    (state / read_target.STATE_FILENAME).write_text(
        json.dumps(
            {
                "current": "alpha-site",
                "bindings": {"alpha-site": {"site": "alpha-site", "face_url": FACE}},
            }
        ),
        encoding="utf-8",
    )


def _answered_report(monkeypatch):
    """A report for a turn a remote model with an unverified placement answered."""
    provider = _make_provider(name="guess", endpoint="https://llm.somewhere.example/v1", priority=1)
    agent = SimpleNamespace(
        gateway=_gateway_with_providers([provider], monkeypatch), session=Session()
    )
    agent.gateway._select_provider("chat")
    return where.where_report(agent)


# ---- the banner --------------------------------------------------------------------------------


def test_bound_banner_names_the_site_and_the_host(isolated):
    _bind(isolated)
    assert where.banner_line() == "BOUND · alpha-site · data and documents from node.example.org"


def test_local_banner_when_this_machine_holds_the_data(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/axiom")
    assert where.banner_line() == "LOCAL · this machine's data platform"


def test_test_mode_banner_wins_over_a_binding(isolated, monkeypatch):
    _bind(isolated)
    monkeypatch.setenv(where.SAMPLE_DATA_ENV, "1")
    assert where.banner_line() == "LOCAL TEST MODE · invented sample data, not a real site"


def test_every_renderer_prints_the_banner_first(isolated, capsys):
    from axiom.extensions.builtins.chat.start_context import sources_banner_line

    _bind(isolated)
    assert sources_banner_line().startswith("BOUND · alpha-site")


# ---- the footer --------------------------------------------------------------------------------


def test_bound_footer_names_both_sources_plainly(isolated, monkeypatch):
    _bind(isolated)
    report = _answered_report(monkeypatch)
    for line in (where.render_stamp(report), where.stamp_from_dict(where.to_dict(report))):
        assert "data: alpha-site via node.example.org" in line
        assert "documents: node.example.org" in line
        assert "REMOTE?" not in line
        assert "RAG: none" not in line
        assert "(unverified)" in line  # a guessed placement is said in words


def test_local_footer_names_this_machine(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/axiom")
    line = where.stamp_from_dict(where.to_dict(_answered_report(monkeypatch)))
    assert "data: this machine" in line and "documents: this machine" in line


def test_no_source_is_said_in_words(monkeypatch):
    line = where.render_stamp(_answered_report(monkeypatch))
    assert "documents: none configured (bind to a site or set rag.database_url)" in line
    assert "RAG: none" not in line


def test_test_mode_marks_every_reply(isolated, monkeypatch):
    monkeypatch.setenv(where.SAMPLE_DATA_ENV, "true")
    report = _answered_report(monkeypatch)
    assert where.render_stamp(report).startswith("↳ TEST MODE (invented sample data)")
    assert where.stamp_from_dict(where.to_dict(report)).startswith("TEST MODE (invented sample data)")
    assert "data: invented sample data" in where.render_stamp(report)


def test_a_dict_from_before_sources_still_renders():
    old = {"placement": "remote-node", "host": "h.example", "verified": True,
           "retrieval": {"kind": "none", "host": ""}}
    assert where.stamp_from_dict(old) == "REMOTE h.example · RAG: none"
