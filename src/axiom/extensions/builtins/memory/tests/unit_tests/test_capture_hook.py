# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""At-turn capture hook — writing when the conversation happens.

Everything before this was a *scrape*: a timer that read whatever the
harness still had on disk. A scrape inherits the retention policy of
whatever it scrapes, silently, because an empty window and a quiet week
look identical. Claude Code's 30-day default deleted months of sessions
before capture ever saw them.

The hook closes that gap. Claude Code fires ``Stop`` when an assistant
response completes and ``SessionEnd`` when a session finishes, handing
the hook a payload containing ``transcript_path``. Capture then happens
because the turn happened, not because a timer fired.

The hard requirement is that this must never damage the session it
observes:

- **Always exit 0.** A non-zero Stop hook is interpreted by the harness;
  a memory backstop must never block or alter the user's conversation.
- **Never raise.** A missing file, malformed payload, or locked ledger
  is a no-op, not a traceback in the user's terminal.
- **Never double-write.** Turns arrive fast and sessions run in
  parallel, so concurrent hook runs must not each write the same turn.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def isolated_composition(tmp_path: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base = tmp_path / "memory"
    base.mkdir()
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _transcript(path: Path, uuid: str = "u-1") -> None:
    lines = [
        {
            "type": "user", "uuid": uuid, "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:00.000Z", "cwd": "/repo",
            "message": {"role": "user",
                        "content": "Always pin the service venv, never the editable one."},
        },
        {
            "type": "assistant", "uuid": "a-1", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:05.000Z",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "text",
                                     "text": "Pinned it to the non-editable install."}]},
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


# ---------------------------------------------------------------------------
# Capture on the event
# ---------------------------------------------------------------------------


def test_hook_ingests_the_transcript_it_is_handed(
    tmp_path: Path, isolated_composition,
):
    from axiom.extensions.builtins.memory.skills.capture_hook import capture_hook

    transcript = tmp_path / "s.jsonl"
    _transcript(transcript)

    result = capture_hook({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "payload": {
            "hook_event_name": "Stop",
            "session_id": "s-1",
            "transcript_path": str(transcript),
        },
        "lock_dir": str(tmp_path / "locks"),
    }, None)

    assert result.ok, result.errors
    assert result.value["written"] == 1


def test_hook_is_idempotent_across_turns(tmp_path: Path, isolated_composition):
    """Stop fires per turn; re-reading the same transcript must not duplicate."""
    from axiom.extensions.builtins.memory.skills.capture_hook import capture_hook

    transcript = tmp_path / "s.jsonl"
    _transcript(transcript)
    params = {
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "payload": {"transcript_path": str(transcript)},
        "lock_dir": str(tmp_path / "locks"),
    }

    assert capture_hook(dict(params), None).value["written"] == 1
    assert capture_hook(dict(params), None).value["written"] == 0


# ---------------------------------------------------------------------------
# Never damage the session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {},
    {"transcript_path": ""},
    {"transcript_path": "/nonexistent/path/to/s.jsonl"},
    {"hook_event_name": "Stop"},
])
def test_bad_payload_is_a_noop_not_a_failure(
    payload, tmp_path: Path, isolated_composition,
):
    from axiom.extensions.builtins.memory.skills.capture_hook import capture_hook

    result = capture_hook({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "payload": payload,
        "lock_dir": str(tmp_path / "locks"),
    }, None)

    assert result.ok
    assert result.value["written"] == 0


def test_corrupt_transcript_does_not_raise(tmp_path: Path, isolated_composition):
    from axiom.extensions.builtins.memory.skills.capture_hook import capture_hook

    bad = tmp_path / "bad.jsonl"
    bad.write_text("{ not json at all\n")

    result = capture_hook({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "payload": {"transcript_path": str(bad)},
        "lock_dir": str(tmp_path / "locks"),
    }, None)
    assert result.ok


def test_cli_hook_always_exits_zero(tmp_path: Path, monkeypatch, capsys):
    """A non-zero Stop hook is interpreted by the harness. Never do that."""
    from axiom.extensions.builtins.memory import cli

    # Isolate HOME: _log_hook_failure appends to ~/.axi/logs, and a unit
    # test must never write into the real capture log — that log is an
    # operator alarm surface, and fake entries make it untrustworthy.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _boom():
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(cli, "_build_default_composition", _boom)

    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"transcript_path": "/nope.jsonl"}))

    rc = cli.main(["hook", "--payload-file", str(payload)])
    assert rc == 0, "hook must never fail the user's turn"


def test_concurrent_hook_run_skips_rather_than_double_writing(
    tmp_path: Path, isolated_composition,
):
    """A held lock means another run is mid-ingest; skip, don't race it."""
    from axiom.extensions.builtins.memory.skills.capture_hook import (
        _acquire_lock,
        capture_hook,
    )

    transcript = tmp_path / "s.jsonl"
    _transcript(transcript)
    lock_dir = tmp_path / "locks"

    holder = _acquire_lock(lock_dir)
    assert holder is not None
    try:
        result = capture_hook({
            "composition": isolated_composition,
            "principal": "ben@example.org",
            "payload": {"transcript_path": str(transcript)},
            "lock_dir": str(lock_dir),
        }, None)
        assert result.ok
        assert result.value["skipped_locked"] is True
        assert result.value["written"] == 0
    finally:
        holder.close()

    # Once free, the same turn is still captured — nothing was lost.
    after = capture_hook({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "payload": {"transcript_path": str(transcript)},
        "lock_dir": str(lock_dir),
    }, None)
    assert after.value["written"] == 1


# ---------------------------------------------------------------------------
# Installation into the harness
# ---------------------------------------------------------------------------


def test_install_registers_stop_and_session_end(tmp_path: Path):
    from axiom.extensions.builtins.memory.hook_install import install_capture_hook

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus", "permissions": {"allow": []}}))

    install_capture_hook(settings_path=settings, axi_path="/usr/local/bin/axi")

    data = json.loads(settings.read_text())
    assert set(data["hooks"]) >= {"Stop", "SessionEnd"}
    # Pre-existing settings survive.
    assert data["model"] == "opus"
    assert data["permissions"] == {"allow": []}


def test_install_is_idempotent(tmp_path: Path):
    from axiom.extensions.builtins.memory.hook_install import install_capture_hook

    settings = tmp_path / "settings.json"
    settings.write_text("{}")

    install_capture_hook(settings_path=settings, axi_path="/usr/local/bin/axi")
    install_capture_hook(settings_path=settings, axi_path="/usr/local/bin/axi")

    data = json.loads(settings.read_text())
    assert len(data["hooks"]["Stop"]) == 1
    assert len(data["hooks"]["Stop"][0]["hooks"]) == 1


def test_install_preserves_unrelated_hooks(tmp_path: Path):
    """Never clobber hooks somebody else installed."""
    from axiom.extensions.builtins.memory.hook_install import install_capture_hook

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/say done"}]}],
            "PreToolUse": [{"hooks": [{"type": "command", "command": "lint.sh"}]}],
        }
    }))

    install_capture_hook(settings_path=settings, axi_path="/usr/local/bin/axi")

    data = json.loads(settings.read_text())
    stop_cmds = [
        h["command"] for entry in data["hooks"]["Stop"] for h in entry["hooks"]
    ]
    assert "/usr/bin/say done" in stop_cmds
    assert any("memory" in c for c in stop_cmds)
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "lint.sh"


def test_uninstall_removes_only_ours(tmp_path: Path):
    from axiom.extensions.builtins.memory.hook_install import (
        install_capture_hook,
        uninstall_capture_hook,
    )

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/say done"}]}],
        }
    }))

    install_capture_hook(settings_path=settings, axi_path="/usr/local/bin/axi")
    uninstall_capture_hook(settings_path=settings)

    data = json.loads(settings.read_text())
    stop_cmds = [
        h["command"] for entry in data["hooks"].get("Stop", []) for h in entry["hooks"]
    ]
    assert stop_cmds == ["/usr/bin/say done"]


def test_install_backs_up_before_writing(tmp_path: Path):
    """Editing the user's live settings warrants a copy first."""
    from axiom.extensions.builtins.memory.hook_install import install_capture_hook

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}))

    result = install_capture_hook(
        settings_path=settings, axi_path="/usr/local/bin/axi",
    )

    backup = Path(result["backup"])
    assert backup.exists()
    assert json.loads(backup.read_text()) == {"model": "opus"}


def test_install_refuses_to_write_malformed_settings(tmp_path: Path):
    """Don't overwrite a file we could not parse — that destroys config."""
    from axiom.extensions.builtins.memory.hook_install import install_capture_hook

    settings = tmp_path / "settings.json"
    settings.write_text("{ this is not json")

    with pytest.raises(ValueError):
        install_capture_hook(settings_path=settings, axi_path="/usr/local/bin/axi")

    assert settings.read_text() == "{ this is not json"


def test_hook_failure_is_actually_logged(tmp_path: Path, monkeypatch):
    """The failure log must really write.

    `_log_hook_failure` swallows every exception so it can never break a
    turn — which also means a NameError inside it would make the hook
    fail *silently*, the exact pattern this whole effort exists to stop.
    Assert the line lands on disk.
    """
    from axiom.extensions.builtins.memory import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _boom():
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(cli, "_build_default_composition", _boom)

    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"transcript_path": "/nope.jsonl"}))

    assert cli.main(["hook", "--payload-file", str(payload)]) == 0

    log = tmp_path / ".axi" / "logs" / "memory-capture.log"
    assert log.exists(), "hook failure was never logged"
    assert "HOOK-FAIL" in log.read_text()
    assert "ledger on fire" in log.read_text()
