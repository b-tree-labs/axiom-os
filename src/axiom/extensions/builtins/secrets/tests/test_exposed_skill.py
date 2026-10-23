# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.exposed`` skill — leaked-credential closer (exposure record + forced rotation)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.secrets.skills import exposed as exposed_skill
from axiom.extensions.builtins.secrets.skills import rotate as rotate_skill
from axiom.infra.skills import SkillContext, default_registry


class FakeStore:
    def __init__(self) -> None:
        self.rotated: list[str] = []
        self.puts: list[bytes] = []

    def rotate(self, ref) -> None:
        self.rotated.append(ref.path)

    def put(self, ref, value) -> None:
        self.puts.append(value)


class RefusingStore(FakeStore):
    def rotate(self, ref) -> None:  # backend refuses -> rotation fails
        raise PermissionError("backend refused rotation")


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    s = FakeStore()
    monkeypatch.setattr(rotate_skill, "_store_for", lambda scheme: s)
    return s


@pytest.fixture
def refusing_store(monkeypatch) -> RefusingStore:
    s = RefusingStore()
    monkeypatch.setattr(rotate_skill, "_store_for", lambda scheme: s)
    return s


def _ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(
        registry=default_registry(),
        state_dir=tmp_path,
        logger=logging.getLogger("test.exposed"),
        user_prompt=None,
    )


def _events(tmp_path: Path) -> list[dict]:
    log = tmp_path / "secrets" / "exposures.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


class TestValidation:
    def test_missing_ref(self, tmp_path):
        result = exposed_skill.run({"where": "transcript"}, _ctx(tmp_path))
        assert not result.ok
        assert any("SecretRef" in e for e in result.errors)

    def test_missing_where(self, tmp_path):
        result = exposed_skill.run({"ref": "openbao://kv/data/x"}, _ctx(tmp_path))
        assert not result.ok
        assert any("--where" in e for e in result.errors)
        assert _events(tmp_path) == []  # nothing recorded for an invalid call

    def test_bad_ref(self, tmp_path):
        result = exposed_skill.run(
            {"ref": "not a ref", "where": "transcript"}, _ctx(tmp_path)
        )
        assert not result.ok


class TestExposureResponse:
    def test_records_exposure_and_forces_rotation(self, store, tmp_path):
        result = exposed_skill.run(
            {
                "ref": "openbao://kv/data/x",
                "where": "transcript",
                "detail": "session abc123, unit-debug echo",
            },
            _ctx(tmp_path),
        )
        assert result.ok
        assert store.rotated  # backend rotation actually ran
        assert result.value["rotation"]["forced"] is True

        events = _events(tmp_path)
        kinds = [e["event"] for e in events]
        assert kinds == ["exposure", "rotation"]
        assert events[0]["where"] == "transcript"
        assert events[0]["detail"] == "session abc123, unit-debug echo"
        assert events[1]["ok"] is True

    def test_force_is_always_on(self, store, tmp_path):
        # even if the caller says force=False, exposure overrides cadence
        result = exposed_skill.run(
            {"ref": "openbao://kv/data/x", "where": "log", "force": False},
            _ctx(tmp_path),
        )
        assert result.ok
        assert result.value["rotation"]["forced"] is True

    def test_exposure_recorded_even_when_rotation_fails(self, refusing_store, tmp_path):
        result = exposed_skill.run(
            {"ref": "openbao://kv/data/x", "where": "transcript"}, _ctx(tmp_path)
        )
        assert not result.ok
        assert any("retried" in e or "rotation" in e for e in result.errors)

        events = _events(tmp_path)
        kinds = [e["event"] for e in events]
        assert kinds == ["exposure", "rotation"]  # the exposure fact survives
        assert events[1]["ok"] is False

    def test_never_echoes_secret_value(self, store, tmp_path):
        secret = "hunter2-super-secret"
        result = exposed_skill.run(
            {
                "ref": "openbao://kv/data/x",
                "where": "chat",
                "strategy": "hitl",
                "value": secret,
            },
            _ctx(tmp_path),
        )
        assert result.ok
        blob = json.dumps(
            {"value": result.value, "actions": result.actions_taken}, default=str
        )
        assert secret not in blob
        assert all(secret not in json.dumps(e) for e in _events(tmp_path))
