# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The program extension is an optional feature that contributes by declaration (prd-program R29).

- Switched off, the platform starts without it: the scheduler host registers
  and runs its declared schedules, the HTTP app composes, the MCP surface
  builds, and nothing imports ``axiom.extensions.builtins.program``. Each
  case runs in a fresh interpreter so the import check sees a clean
  ``sys.modules``.
- Switched on (by ``features enable program``, or because the node already
  holds program state), the same host registers its sync, consolidation and
  follow-up schedules from the manifest and binds its skills, naming nothing.
- The switch: default off on a fresh node, on where program state exists, an
  explicit ``off`` wins over that evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from axiom.infra import node_functions as nf
from axiom.infra import optional_extensions as oe

_PROBE = textwrap.dedent(
    """
    import contextlib, json, sys
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from axiom.extensions.builtins.schedule import store as pulse_store
    from axiom.extensions.builtins.schedule.db_models import Base, ScheduleDefinition

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine)

    @contextlib.contextmanager
    def provider():
        s = maker()
        try:
            yield s
        finally:
            s.close()

    pulse_store.set_provider(provider)

    from axiom.extensions.builtins.data_platform.services.orchestrator import OrchestratorService
    from axiom.infra.paths import get_user_state_dir

    svc = OrchestratorService(state_dir=get_user_state_dir(), node_id="probe")
    report = svc.ensure_declared_schedules()
    svc.tick()
    with provider() as s:
        actions = sorted(r.action for r in s.query(ScheduleDefinition) if r.state == "active")

    from axiom.extensions.builtins.http.compose import compose_app, discovered_manifests
    from axiom.extensions.builtins.http.registry import RouterRegistry

    mounts = RouterRegistry()
    app = compose_app(registry=mounts, manifests=discovered_manifests(), auto_authz=False,
                      allow_insecure=True, bind_host="127.0.0.1")
    routes = sorted(s.prefix for s in mounts.specs())

    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
    from axiom.infra.skills import SkillRegistry

    surface = AggregationRegistry.from_node().build()
    tools = sorted(t.name for t in surface.tools)

    print("PROBE" + json.dumps({
        "loaded": sorted(m for m in sys.modules if m.startswith("axiom.extensions.builtins.program")),
        "actions": actions,
        "errors": report["errors"],
        "program_routes": [r for r in routes if r.startswith("/program")],
        "program_tools": [t for t in tools if t.startswith("axiom_program")],
        "tools": len(tools),
        "mounts": len(routes),
    }))
    """
)


def _probe(tmp_path: Path, *, node_toml: str = "", program_state: bool = False) -> dict:
    state = tmp_path / "state"
    state.mkdir()
    if program_state:
        (state / "program").mkdir()
    node = tmp_path / "node.toml"
    if node_toml:
        node.write_text(node_toml, encoding="utf-8")
    env = {
        **os.environ,
        "AXI_STATE_DIR": str(state),
        "AXIOM_NODE_CONFIG": str(node),
        "AXI_NO_SOURCE_WARNING": "1",
        "AXI_INDEX_CACHE": "0",
    }
    out = subprocess.run([sys.executable, "-c", _PROBE], env=env, capture_output=True, text=True, timeout=240)
    line = next((ln for ln in out.stdout.splitlines() if ln.startswith("PROBE")), None)
    assert line is not None, f"probe failed (rc={out.returncode}):\n{out.stdout[-3000:]}\n{out.stderr[-6000:]}"
    return json.loads(line[len("PROBE"):])


@pytest.mark.timeout(300)
def test_switched_off_the_platform_runs_and_never_imports_program(tmp_path):
    seen = _probe(tmp_path)
    assert seen["loaded"] == [], seen["loaded"]
    assert not any(a.startswith("program.") for a in seen["actions"]), seen["actions"]
    assert seen["program_routes"] == [] and seen["program_tools"] == []
    assert seen["tools"] > 0 and seen["mounts"] > 0  # the rest of the platform is still there


@pytest.mark.timeout(300)
def test_an_explicit_off_wins_over_existing_program_state(tmp_path):
    seen = _probe(tmp_path, node_toml='[node]\nfunctions = []\nfeatures = []\noff = ["program"]\n', program_state=True)
    assert seen["loaded"] == [] and seen["program_tools"] == []


@pytest.mark.timeout(300)
def test_switched_on_it_contributes_its_schedules_routes_and_tools_by_declaration(tmp_path):
    seen = _probe(tmp_path, node_toml='[node]\nfunctions = []\nfeatures = ["program"]\n')
    assert {"program.sync", "program.consolidate", "program.followup_sweep"} <= set(seen["actions"])
    assert seen["program_routes"] and "axiom_program__status" in seen["program_tools"]
    assert seen["errors"] == []


class TestTheSwitch:
    BLOCK = {"default": "off", "in_use_when": ["program"]}

    def test_fresh_node_is_off(self, tmp_path):
        assert not oe.is_enabled("program", self.BLOCK, cfg=nf.NodeConfig(), state_dir=tmp_path)

    def test_existing_program_state_keeps_a_live_node_on(self, tmp_path):
        (tmp_path / "program").mkdir()
        assert oe.is_enabled("program", self.BLOCK, cfg=nf.NodeConfig(), state_dir=tmp_path)

    def test_enable_and_an_explicit_off(self, tmp_path):
        (tmp_path / "program").mkdir()
        assert oe.is_enabled("program", self.BLOCK, cfg=nf.NodeConfig(features=("program",)), state_dir=tmp_path)
        assert not oe.is_enabled("program", self.BLOCK, cfg=nf.NodeConfig(off=("program",)), state_dir=tmp_path)

    def test_a_non_optional_extension_is_always_on(self, tmp_path):
        assert oe.is_enabled("anything", None, cfg=nf.NodeConfig(off=("anything",)), state_dir=tmp_path)

    def test_evidence_paths_cannot_escape_the_state_dir(self, tmp_path):
        (tmp_path / "x").mkdir()
        assert not oe.in_use({"in_use_when": ["../x", "/etc"]}, tmp_path / "state")

    def test_features_enable_and_disable_round_trip(self, tmp_path):
        path = tmp_path / "node.toml"
        cfg = nf.enable("program", known={"program": {}}, path=path)
        assert "program" in cfg.features and "program" not in cfg.off
        cfg = nf.disable("program", path=path, optional=True)
        assert "program" not in cfg.features and "program" in cfg.off
        cfg = nf.enable("program", known={"program": {}}, path=path)
        assert "program" in cfg.features and cfg.off == ()

    def test_the_manifest_declares_it_optional_with_hosted_schedules(self):
        from axiom.infra.toml_compat import tomllib

        manifest = Path(__file__).resolve().parents[1] / "axiom-extension.toml"
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        assert oe.optional_block(data) == {"default": "off", "in_use_when": ["program"]}
        hosted = {b["action"] for b in data["extension"]["schedule"] if b.get("hosted")}
        assert hosted == {"program.sync", "program.consolidate", "program.followup_sweep"}

    def test_the_orchestrator_source_names_no_program_module(self):
        from axiom.extensions.builtins.data_platform.services import orchestrator

        assert "builtins.program" not in Path(orchestrator.__file__).read_text(encoding="utf-8")
