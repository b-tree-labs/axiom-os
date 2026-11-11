# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.consolidate`` / ``program.digest``: one digest per cycle, proposals only."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from axiom.extensions.builtins.program.skills import consolidate
from axiom.infra.skills import SkillContext, SkillRegistry

from .conftest import GENERIC_PROGRAM

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def node(tmp_path, monkeypatch):
    state = tmp_path / "node"
    (state / "program").mkdir(parents=True)
    raw = json.loads(json.dumps(GENERIC_PROGRAM))
    # i-four has no owner: one standing proposal
    (state / "program" / "data.json").write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(consolidate, "_now", lambda: NOW)
    sent: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(consolidate, "_sender", lambda *a: (sent.append(a), SimpleNamespace(ok=True, reach="pushed"))[1])
    monkeypatch.delenv("AXI_FOLLOWUP_OPERATOR", raising=False)
    ctx = SkillContext(registry=SkillRegistry(), state_dir=state, logger=logging.getLogger("t"), user_prompt=None)
    return SimpleNamespace(state=state, ctx=ctx, sent=sent, data=state / "program" / "data.json")


def _edit(path: Path, fn) -> None:
    raw = json.loads(path.read_text())
    fn(raw)
    path.write_text(json.dumps(raw))


def test_a_pass_persists_one_digest_and_writes_nothing_else(node):
    before = node.data.read_text()
    out = consolidate.run({}, node.ctx)
    assert out.ok and out.value["count"] >= 1
    assert "missing_owner" in out.value["by_kind"]
    assert node.data.read_text() == before  # proposals only
    latest = json.loads((node.state / "program" / "digests" / "latest.json").read_text())
    assert latest["digest_id"] == out.value["digest_id"]
    assert node.sent == []  # not asked to deliver


def test_deliver_sends_once_to_the_deputy_and_not_again_when_unchanged(node):
    first = consolidate.run({"deliver": True}, node.ctx)
    assert first.value["delivery"]["sent"] and first.value["delivery"]["recipient"] == "@casey:example-org"
    assert len(node.sent) == 1 and node.sent[0][3].startswith("program.digest:example-program:")
    again = consolidate.run({"deliver": True}, node.ctx)
    assert again.value["delivery"] == {"sent": False, "reason": "unchanged since the last delivered digest"}
    assert len(node.sent) == 1


def test_an_empty_digest_is_not_sent(node):
    def tidy(raw: dict[str, Any]) -> None:
        raw["schedule"] = [i for i in raw["schedule"] if i.get("owner")]

    _edit(node.data, tidy)
    out = consolidate.run({"deliver": True}, node.ctx)
    assert out.value["count"] == 0 and out.value["delivery"]["sent"] is False and node.sent == []


def test_a_non_writer_install_never_delivers(node):
    _edit(node.data, lambda raw: raw["program"].update({"writer": "writer-node"}))
    out = consolidate.run({"deliver": True, "_node_id": "laptop"}, node.ctx)
    assert out.value["delivery"]["sent"] is False and "read_only" in out.value["delivery"]
    assert node.sent == []


def test_the_scheduled_pass_is_quiet_without_a_program(tmp_path):
    ctx = SkillContext(registry=SkillRegistry(), state_dir=tmp_path / "empty", logger=logging.getLogger("t"))
    out = consolidate.run({"scheduled": True, "deliver": True}, ctx)
    assert out.ok and out.value["skipped_run"] == "no_program"


def test_digest_reads_the_latest_and_filters_by_kind(node):
    assert consolidate.digest({}, node.ctx).value["refused"] == "absent"
    consolidate.run({}, node.ctx)
    only = consolidate.digest({"kind": "missing_owner"}, node.ctx)
    assert only.ok and {p["kind"] for p in only.value["proposals"]} == {"missing_owner"}
    assert consolidate.digest({"kind": "nope"}, node.ctx).value["refused"] == "bad_request"


def test_history_is_bounded(node, monkeypatch):
    for day in range(1, consolidate.HISTORY + 4):
        monkeypatch.setattr(consolidate, "_now", lambda d=day: datetime(2026, 10, d, tzinfo=UTC))
        consolidate.run({}, node.ctx)
    kept = [p for p in (node.state / "program" / "digests").glob("*.json") if p.name != "latest.json"]
    assert len(kept) == consolidate.HISTORY


def test_the_message_lists_proposals_and_points_at_the_full_digest(node):
    digest = consolidate.run({}, node.ctx).value
    subject, body = consolidate.render_message(digest)
    assert "proposal" in subject and "nothing has been changed" in body and "program digest" in body


def test_the_skills_are_registered_with_the_right_surfaces():
    from axiom.extensions.builtins.mcp.skill_tools import is_mcp_exposed
    from axiom.extensions.builtins.program import skills

    registry = SkillRegistry()
    skills.bind(registry)
    specs = registry.specs()
    assert not is_mcp_exposed(specs["program.consolidate"]) and specs["program.consolidate"].side_effects
    assert is_mcp_exposed(specs["program.digest"]) and not specs["program.digest"].side_effects


def test_the_cli_runs_consolidate(node, monkeypatch, capsys):
    from axiom.extensions.builtins.program import cli

    monkeypatch.setattr(cli, "get_user_state_dir", lambda: node.state)
    assert cli.main(["consolidate", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["value"]["count"] >= 1
    assert cli.main(["digest", "--kind", "missing_owner", "--json"]) == 0
