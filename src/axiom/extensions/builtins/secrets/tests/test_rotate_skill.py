# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.rotate`` skill + CLI wiring (ADR-095 / ADR-056)."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    s = FakeStore()
    monkeypatch.setattr(rotate_skill, "_store_for", lambda scheme: s)
    return s


def _ctx(user_prompt=None) -> SkillContext:
    return SkillContext(
        registry=default_registry(),
        state_dir=Path("/tmp"),
        logger=logging.getLogger("test.rotate"),
        user_prompt=user_prompt,
    )


class TestValidation:
    def test_missing_ref(self, store):
        r = rotate_skill.run({}, _ctx())
        assert not r.ok and "SecretRef" in r.errors[0]

    def test_bad_ref(self, store):
        r = rotate_skill.run({"ref": "no-scheme"}, _ctx())
        assert not r.ok

    def test_vendor_strategy_needs_config(self, store):
        r = rotate_skill.run(
            {"ref": "openbao://kv/x", "strategy": "sendgrid"}, _ctx()
        )
        assert not r.ok and "vendor-API" in r.errors[0]

    def test_unknown_strategy(self, store):
        r = rotate_skill.run(
            {"ref": "openbao://kv/x", "strategy": "bogus"}, _ctx()
        )
        assert not r.ok and "unknown strategy" in r.errors[0]


class TestProviderNative:
    def test_rotates_via_backend(self, store):
        r = rotate_skill.run(
            {"ref": "openbao://kv/db-pw", "strategy": "provider-native", "force": True},
            _ctx(),
        )
        assert r.ok
        assert store.rotated == ["kv/db-pw"]
        assert r.value["strategy"] == "provider-native"
        assert r.value["forced"] is True
        # never leaks a value
        assert "value" not in r.value and "secret" not in r.value


class TestHitl:
    def test_headless_without_value_fails_cleanly(self, store):
        r = rotate_skill.run(
            {"ref": "openbao://kv/gh-pat", "strategy": "hitl", "force": True},
            _ctx(user_prompt=None),
        )
        assert not r.ok  # HitlRotation refuses an empty value

    def test_value_flag_stages_new_credential(self, store):
        r = rotate_skill.run(
            {"ref": "openbao://kv/gh-pat", "strategy": "hitl",
             "force": True, "value": "ghp_newtoken"},
            _ctx(),
        )
        assert r.ok
        assert store.puts == [b"ghp_newtoken"]
        assert r.value["strategy"] == "hitl"

    def test_interactive_prompt_supplies_value(self, store):
        r = rotate_skill.run(
            {"ref": "openbao://kv/gh-pat", "strategy": "hitl", "force": True},
            _ctx(user_prompt=lambda p: "pasted-secret"),
        )
        assert r.ok
        assert store.puts == [b"pasted-secret"]


class TestCadenceGate:
    def test_not_due_without_force(self, store):
        r = rotate_skill.run(
            {"ref": "openbao://kv/x", "strategy": "provider-native",
             "cadence": 100000, "last_rotated_at": 9_000_000_000.0},
            _ctx(),
        )
        assert not r.ok  # future last-rotated → not due, no force → refused


def test_cli_subprocess_smoke():
    """E2E through the argparse CLI: headless hitl with no value exits non-zero
    with a clear error (no store needed — it fails before touching one)."""
    proc = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.secrets.cli",
         "--json", "rotate", "openbao://kv/x", "--strategy", "hitl", "--force"],
        capture_output=True, text=True, timeout=60,
        env={"PATH": __import__("os").environ.get("PATH", ""),
             "PYTHONPATH": str(Path(__file__).resolve().parents[5])},  # .../src
    )
    assert proc.returncode == 1, proc.stderr
    assert '"ok": false' in proc.stdout.lower() or "ok': false" in proc.stdout.lower()
