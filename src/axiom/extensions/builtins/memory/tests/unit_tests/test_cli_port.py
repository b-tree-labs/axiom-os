# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axi memory port`` — thin ADR-056 wrapper over memory.port.

The skill's orchestration contract is covered in test_port_skill.py;
these pin the CLI mapping: flags → params, JSON passthrough, rendering,
and exit codes.
"""

from __future__ import annotations

import json
from importlib import import_module
from typing import Any

import pytest

from axiom.infra.skills import SkillResult

PRINCIPAL = "@alice:personal"

_OK_VALUE: dict[str, Any] = {
    "principal": PRINCIPAL,
    "refresh": False,
    "dry_run": False,
    "mcp": {"results": {"claude-code": {"action": "added"}}},
    "harnesses": {
        "claude-code": {"imported": 2, "skipped_echo": 1},
    },
    "bundle": None,
    "reindex": None,
}


@pytest.fixture
def stub_port(monkeypatch):
    """Replace the port skill; record the params the CLI hands it."""
    seen: dict[str, Any] = {}
    port_mod = import_module("axiom.extensions.builtins.memory.skills.port")

    def fake_port(params, ctx):
        seen.update(params)
        return SkillResult(value=dict(_OK_VALUE))

    monkeypatch.setattr(port_mod, "port", fake_port)
    # The handler builds the composition before invoking the skill; keep
    # the test hermetic (no user state dir).
    from axiom.extensions.builtins.memory import cli

    monkeypatch.setattr(cli, "_build_default_composition", lambda: object())
    return seen


def _main(argv: list[str]) -> int:
    from axiom.extensions.builtins.memory import cli

    return cli.main(argv)


class TestCliPort:
    def test_flags_map_to_skill_params(self, stub_port):
        rc = _main([
            "port", "--principal", PRINCIPAL, "--account", "work",
            "--harness", "claude-code", "--harness", "codex",
            "--root", "/repo/a", "--home", "/fake/home",
            "--bundle", "/x/b.tar.gz", "--sessions-dir", "/x/s",
            "--refresh", "--dry-run",
        ])
        assert rc == 0
        assert stub_port["principal"] == PRINCIPAL
        assert stub_port["account"] == "work"
        assert stub_port["harnesses"] == ["claude-code", "codex"]
        assert stub_port["roots"] == ["/repo/a"]
        assert stub_port["home"] == "/fake/home"
        assert stub_port["bundle"] == "/x/b.tar.gz"
        assert stub_port["sessions_dir"] == "/x/s"
        assert stub_port["refresh"] is True
        assert stub_port["dry_run"] is True
        assert stub_port["composition"] is not None

    def test_json_output_is_the_skill_value(self, stub_port, capsys):
        rc = _main(["port", "--principal", PRINCIPAL, "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["principal"] == PRINCIPAL
        assert payload["harnesses"]["claude-code"]["imported"] == 2

    def test_human_rendering(self, stub_port, capsys):
        rc = _main(["port", "--principal", PRINCIPAL])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Ported" in out
        assert "claude-code" in out

    def test_skill_failure_exits_nonzero_with_errors(self, monkeypatch, capsys):
        port_mod = import_module("axiom.extensions.builtins.memory.skills.port")
        monkeypatch.setattr(
            port_mod, "port",
            lambda params, ctx: SkillResult(ok=False, errors=["boom"]),
        )
        from axiom.extensions.builtins.memory import cli

        monkeypatch.setattr(cli, "_build_default_composition", lambda: object())
        rc = _main(["port", "--principal", PRINCIPAL])
        assert rc == 1
        assert "boom" in capsys.readouterr().err

    def test_unresolvable_principal_is_a_clean_error(self, monkeypatch, capsys):
        import axiom.memory.session_capture as sc

        def raise_value_error(_p):
            raise ValueError("no default principal configured")

        monkeypatch.setattr(sc, "resolve_principal_id", raise_value_error)
        rc = _main(["port"])
        assert rc == 1
        assert "no default principal" in capsys.readouterr().err
