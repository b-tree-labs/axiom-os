# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.export`` is one definition: the terminal, an agent and chat all get it from the registry."""

from __future__ import annotations

from axiom.extensions.builtins.data_platform import skills
from axiom.extensions.builtins.data_platform.commands import cli
from axiom.extensions.builtins.data_platform.skills import gold
from axiom.extensions.builtins.mcp import skill_tools
from axiom.infra.skills import SkillRegistry, SkillResult


def _spec(name):
    reg = SkillRegistry()
    skills.bind(reg)
    return reg.get_spec(name) if hasattr(reg, "get_spec") else reg.spec(name)


def test_export_is_declared_a_write_on_every_surface_and_verify_a_read():
    export, verify = _spec("data.export"), _spec("data.export_verify")
    for spec in (export, verify):
        assert {"cli", "mcp", "agent_tool"} <= set(spec.surfaces)
    # It writes a file where the caller is: an assistant asks before it does.
    assert export.side_effects is True and export.risk == "reversible-write"
    assert verify.side_effects is False and verify.risk == "read"


def test_an_agent_sees_both_tools_with_the_inputs_a_person_types():
    export = _spec("data.export")
    assert skill_tools.is_mcp_exposed(export)
    assert skill_tools.mcp_tool_name(export.name).endswith("data__export")
    for name in ("dataset", "channels", "start", "end", "format", "out", "rows", "columns"):
        assert name in export.inputs
    # A caller cannot widen its own access tiers through an export.
    assert "tiers" not in export.inputs and "dsn" not in export.inputs


def test_the_terminal_verb_resolves_a_day_and_reaches_the_same_skill(tmp_path, monkeypatch, capsys):
    seen = []

    def table(params, ctx):
        seen.append(params)
        return SkillResult(ok=True, value={
            "rows": [{"ts": "2026-03-04T14:00:00Z", "channel": "loop.flow", "value": 1.5,
                      "unit": "kg/s", "source_class": "measured", "site": "example-site"}],
            "next": None, "spec": {"columns": [{"id": c} for c in
                                               ("ts", "channel", "value", "unit", "source_class", "site")]},
        })

    monkeypatch.setattr(gold, "table", table)
    out = tmp_path / "day.csv"
    code = cli.main(["export", "--channels", "loop.flow", "--day", "2026-03-04", "--out", str(out)])
    assert code == 0
    printed = capsys.readouterr().out
    assert f"wrote {out}" in printed and "sha256" in printed
    where = {(w["column"], w["op"]): w["value"] for w in seen[0]["where"]}
    assert where[("channel", "in")] == ["loop.flow"]
    assert str(where[("ts", "ge")]).startswith("2026-03-04") and ("ts", "lt") in where
    assert seen[0]["dataset"] == "signals"

    assert cli.main(["export-verify", str(out)]) == 0
    assert "holds the 1 rows" in capsys.readouterr().out
    out.write_text(out.read_text().replace("1.5", "2.5"))
    assert cli.main(["export-verify", str(out)]) == 1
