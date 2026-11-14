# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A pending diagnosis is a claim about the present, so it is re-checked before it is shown (#812).

A matched diagnosis used to be true forever as far as the surface was concerned: it kept advising a
fix for a problem that was gone. Patterns that can be checked mechanically carry the evidence the
check needs; when the trigger no longer reproduces, the record retires itself. A pattern with no
check, or a record that predates the evidence, is kept: an unknown is never treated as resolved.
"""

from __future__ import annotations

import json
import socket

import pytest

from axiom.extensions.builtins.diagnostics import cli_diagnoses, cli_listener


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AXI_DIAGNOSES_QUIET", raising=False)


def _llamafile_event(port: int) -> dict:
    return {
        "command": "chat",
        "error_message": f"Connection refused: http://127.0.0.1:{port}/v1/chat/completions",
        "traceback": "gateway llm completion",
    }


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _record(pattern_id="p", fp="fp1", evidence=None) -> dict:
    d = {
        "fingerprint": fp,
        "pattern_id": pattern_id,
        "summary": "s",
        "remedy": "r",
        "confidence": 0.9,
        "matched_at": "2099-01-01T00:00:00Z",
    }
    if evidence is not None:
        d["evidence"] = evidence
    return d


def test_a_llamafile_diagnosis_records_the_endpoint_it_saw():
    port = _closed_port()
    dx = cli_diagnoses.match_failure(_llamafile_event(port))
    assert dx is not None and dx.pattern_id == "local-llamafile-down"
    assert dx.to_dict()["evidence"] == {"host": "127.0.0.1", "port": port}


def test_it_retires_when_the_endpoint_is_listening_again():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        rec = _record("local-llamafile-down", evidence={"host": "127.0.0.1", "port": port})
        cli_listener.append_diagnosis(None, rec)
        retired = cli_listener.retire_resolved()
    assert [d["fingerprint"] for d in retired] == ["fp1"]
    assert cli_listener.read_pending() == []


def test_it_stays_while_the_endpoint_is_still_down():
    rec = _record("local-llamafile-down", evidence={"host": "127.0.0.1", "port": _closed_port()})
    cli_listener.append_diagnosis(None, rec)
    assert cli_listener.retire_resolved() == []
    assert len(cli_listener.read_pending()) == 1


def test_an_unknown_is_kept():
    """No check for the pattern, or no evidence on an old record: never treated as resolved."""
    cli_listener.append_diagnosis(None, _record("llm-fallback:abc", fp="a"))
    cli_listener.append_diagnosis(None, _record("local-llamafile-down", fp="b"))  # no evidence
    assert cli_listener.retire_resolved() == []
    assert len(cli_listener.read_pending()) == 2


def test_a_check_that_raises_keeps_the_record(monkeypatch):
    def boom(_evidence):
        raise RuntimeError("probe broke")

    monkeypatch.setitem(cli_diagnoses.REVALIDATORS, "p", boom)
    cli_listener.append_diagnosis(None, _record("p"))
    assert cli_listener.retire_resolved() == []
    assert len(cli_listener.read_pending()) == 1


def test_the_triage_pointer_does_not_show_a_resolved_diagnosis(capsys):
    from axiom.infra.cli_hooks import surface_pending_diagnoses

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        cli_listener.append_diagnosis(
            None, _record("local-llamafile-down", evidence={"host": "127.0.0.1", "port": port})
        )
        surface_pending_diagnoses("triage sweep")
    assert capsys.readouterr().err == ""
    assert not json.dumps(cli_listener.read_pending()).count("fp1")
