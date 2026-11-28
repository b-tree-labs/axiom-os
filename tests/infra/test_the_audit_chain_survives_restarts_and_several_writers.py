# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One audit chain per node, whatever restarts and however many units write (#2470).

Found installing the signing key on a node: every process began its chain at
GENESIS, the log directory followed the working directory, and the command the
warning named did not exist. A restart, or a second unit, then read as
tampering.
"""

from __future__ import annotations

import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from axiom.infra import audit_log
from axiom.infra.audit_log import AuditLog

KEY = "k" * 64


def _routing(log: AuditLog, n: int) -> None:
    log.write_routing(
        session_id=f"s{n}",
        tier_requested="export_controlled",
        tier_assigned="export_controlled",
        provider_name="local",
        provider_tier="export_controlled",
        blocked=False,
        block_reason=None,
        prompt_hash=f"p{n}",
        response_hash=None,
        ec_violation=False,
        is_ec=True,
    )


def _ec_log(directory: Path) -> AuditLog:
    log = AuditLog(log_dir=directory, hmac_key=KEY)
    log.set_mode("ec")
    return log


def test_a_restarted_process_continues_the_chain(tmp_path):
    first = _ec_log(tmp_path)
    _routing(first, 1)
    _routing(first, 2)
    restarted = _ec_log(tmp_path)  # a new process: nothing carried in memory
    _routing(restarted, 3)
    assert restarted.verify_chain() == (True, None)
    assert len(restarted._backend.read_ordered("routing_events")) == 3


def test_two_writers_taking_turns_keep_one_chain(tmp_path):
    serve, rag = _ec_log(tmp_path), _ec_log(tmp_path)
    for n in range(6):
        _routing(serve if n % 2 else rag, n)
    assert serve.verify_chain() == (True, None)


def _writer(directory: str, start: int) -> None:
    log = _ec_log(Path(directory))
    for n in range(start, start + 25):
        _routing(log, n)


def test_processes_writing_at_once_keep_one_chain(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_writer, args=(str(tmp_path), i * 100)) for i in range(3)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(60)
        assert w.exitcode == 0
    log = _ec_log(tmp_path)
    assert len(log._backend.read_ordered("routing_events")) == 75
    assert log.verify_chain() == (True, None)


def test_a_changed_record_is_still_caught(tmp_path):
    log = _ec_log(tmp_path)
    for n in range(3):
        _routing(log, n)
    path = tmp_path / "routing_events.jsonl"
    path.write_text(path.read_text().replace('"p1"', '"pX"'))
    assert _ec_log(tmp_path).verify_chain() == (False, 1)


def test_a_removed_record_is_still_caught(tmp_path):
    log = _ec_log(tmp_path)
    for n in range(3):
        _routing(log, n)
    path = tmp_path / "routing_events.jsonl"
    lines = path.read_text().splitlines(keepends=True)
    path.write_text(lines[0] + lines[2])
    assert _ec_log(tmp_path).verify_chain() == (False, 1)


def test_the_log_lives_in_the_state_directory_not_the_working_directory(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.delenv("AXIOM_AUDIT_LOG_DIR", raising=False)
    for cwd in ("unit-a", "unit-b"):
        (tmp_path / cwd).mkdir()
        monkeypatch.chdir(tmp_path / cwd)
        assert audit_log.default_log_dir() == state / "audit"


def test_a_node_can_name_the_audit_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_AUDIT_LOG_DIR", str(tmp_path / "elsewhere"))
    assert audit_log.default_log_dir() == tmp_path / "elsewhere"


def _key_file(tmp_path, monkeypatch, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / "audit-hmac-key.env"
    path.write_text(text)
    path.chmod(mode)
    monkeypatch.setenv("AXIOM_AUDIT_HMAC_KEY_FILE", str(path))
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    return path


def test_the_key_is_read_from_the_key_file_when_the_environment_has_none(tmp_path, monkeypatch):
    _key_file(tmp_path, monkeypatch, f"# audit chain key\nAXIOM_AUDIT_HMAC_KEY={KEY}\n")
    assert audit_log.signing_key() == KEY


def test_the_environment_wins_over_the_key_file(tmp_path, monkeypatch):
    _key_file(tmp_path, monkeypatch, f"AXIOM_AUDIT_HMAC_KEY={KEY}\n")
    monkeypatch.setenv("AXIOM_AUDIT_HMAC_KEY", "from-env")
    assert audit_log.signing_key() == "from-env"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_a_key_file_others_can_read_is_not_used(tmp_path, monkeypatch, caplog):
    path = _key_file(tmp_path, monkeypatch, f"AXIOM_AUDIT_HMAC_KEY={KEY}\n", mode=0o644)
    assert audit_log.signing_key() is None
    assert str(path) in caplog.text and "chmod 600" in caplog.text
    assert KEY not in caplog.text


def test_setup_audit_key_writes_a_private_key_file_and_never_prints_it(
    tmp_path, monkeypatch, capsys
):
    from axiom.extensions.builtins.setup import audit_key

    path = tmp_path / "config" / "audit-hmac-key.env"
    monkeypatch.setenv("AXIOM_AUDIT_HMAC_KEY_FILE", str(path))
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))

    assert audit_key.main([]) == 0
    key = audit_log.signing_key()
    assert key and len(key) == 64
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    out = capsys.readouterr()
    assert key not in out.out + out.err
    assert str(path) in out.out and "restart" in out.out.lower()

    # A second run keeps the key: a new one would orphan every record already signed.
    assert audit_key.main([]) == 0
    assert audit_log.signing_key() == key
    assert "already" in capsys.readouterr().out


def test_setup_audit_key_is_the_command_the_warning_names(monkeypatch):
    from axiom.extensions.builtins.setup import cli

    called = []
    monkeypatch.setattr(
        "axiom.extensions.builtins.setup.audit_key.main", lambda argv: called.append(argv) or 0
    )
    assert cli.main(["audit-key"]) == 0
    assert called == [[]]
