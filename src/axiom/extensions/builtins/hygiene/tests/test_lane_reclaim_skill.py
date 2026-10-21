# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The agent-reachable face of lane-reclaim: ``axi hygiene reclaim``.

``lane_reclaim.assess`` is unit-tested next door; this covers the wiring that
makes it a skill — registered, projected to the surfaces TIDY can reach from
Claude Code, propose-only on every one of them, and honest when the extensions
it leans on are absent.
"""

from __future__ import annotations

import json
import logging

import pytest

from axiom.extensions.builtins.hygiene import skills as H
from axiom.extensions.builtins.hygiene import cli
from axiom.extensions.builtins.lane.registry import Lane
from axiom.infra import capability_projection as cp
from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext


@pytest.fixture
def lanes_file(tmp_path, monkeypatch):
    """A lane registry with one gone checkout (reclaimable) and one live one."""
    live = tmp_path / "live"
    live.mkdir()
    lanes = {
        # `isolated` is derived from having a database, not a constructor field.
        "gone": Lane(name="gone", front=8810, api=8811, database="axiom_lane_gone",
                     branch="feat/gone", root=str(tmp_path / "vanished")),
        "live": Lane(name="live", front=8812, api=8813, database="axiom_lane_live",
                     branch="feat/live", root=str(live)),
    }
    path = tmp_path / "lanes.json"
    path.write_text(json.dumps({"lanes": {n: vars(lane) for n, lane in lanes.items()}}))
    monkeypatch.setenv("AXIOM_LANES_FILE", str(path))
    monkeypatch.setenv("AXI_WORKSPACE_ROOT", str(tmp_path))
    return path


def _ctx(reg=None):
    return SkillContext(registry=reg, state_dir=get_user_state_dir(),
                        logger=logging.getLogger("t"))


def test_reclaim_is_registered_read_only_on_every_surface():
    reg = H.bind_default()
    spec = reg.spec("hygiene.reclaim")
    assert cp.is_read_only(spec)
    assert cp.exposed_on(spec, "cli")
    assert cp.exposed_on(spec, "mcp")
    assert cp.exposed_on(spec, "agent_tool")
    # The MCP/agent surface name an agent in Claude Code calls it by.
    assert cp.capability_to_surface_name(spec.name) == "hygiene__reclaim"


def test_reclaim_proposes_the_gone_lane_and_blocks_the_live_one(lanes_file):
    from axiom.extensions.builtins.hygiene.skills import reclaim

    res = reclaim.run({}, _ctx())
    assert res.ok
    assert [r["lane"] for r in res.value["reclaimable"]] == ["gone"]
    # A live checkout whose branch RIVET cannot vouch for is blocked, not swept.
    assert [r["lane"] for r in res.value["blocked"]] == ["live"]


def test_the_proposal_prints_the_drop_but_the_skill_runs_nothing(lanes_file):
    from axiom.extensions.builtins.hygiene.skills import reclaim

    res = reclaim.run({}, _ctx())
    gone = res.value["reclaimable"][0]
    assert gone["proposed"][0] == "axi lane release gone"
    assert any("dropdb axiom_lane_gone" in c and "irreversible" in c for c in gone["proposed"])
    # Propose-only: a read skill records no actions beyond the rendered report.
    assert res.value["text"] in res.actions_taken


def test_cli_json_is_the_headless_payload(lanes_file, capsys):
    rc = cli.main(["--json", "reclaim"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["lane"] for r in out["reclaimable"]] == ["gone"]


def test_without_a_lane_registry_there_is_simply_nothing_to_reclaim(monkeypatch, tmp_path):
    # Point at a registry that cannot be read; the skill degrades, never raises.
    missing = tmp_path / "nope" / "lanes.json"
    monkeypatch.setenv("AXIOM_LANES_FILE", str(missing))
    from axiom.extensions.builtins.hygiene.skills import reclaim

    res = reclaim.run({}, _ctx())
    assert res.ok
    assert res.value["reclaimable"] == [] and res.value["blocked"] == []
