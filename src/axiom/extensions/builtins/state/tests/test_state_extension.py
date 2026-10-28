# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi state`` -- skills, CLI wrappers, manifest, schedule, wiring.  Temp trees only."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from axiom.extensions.builtins.state import cli as state_cli
from axiom.extensions.builtins.state import skills as state_skills
from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    for k in ("AXI_STATE_DIR", "AXIOM_STATE_DIR"):
        monkeypatch.setenv(k, str(state))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    assert str(state).startswith(str(tmp_path))
    (state / "identity").mkdir()
    (state / "identity" / "identity.json").write_text('{"owner":"x"}')
    (state / "settings.toml").write_text('a = 1\n')
    (state / "sessions").mkdir()
    (state / "sessions" / "s.json").write_text("{}")
    return state


def _ctx(state: Path) -> SkillContext:
    reg = SkillRegistry()
    state_skills.bind(reg)
    return SkillContext(registry=reg, state_dir=state, logger=logging.getLogger("t"))


def test_every_verb_is_a_registered_skill_and_reads_are_not_audited():
    reg = SkillRegistry()
    state_skills.bind(reg)
    for verb in ("snapshot", "ls", "show", "diff", "verify", "restore", "prune", "pin", "unpin", "snapshot_tick"):
        assert reg.has(f"state.{verb}"), verb
    for read in ("ls", "show", "diff", "verify"):
        assert reg._mutating[f"state.{read}"] is False
    # only restore changes harness state, so only restore is journaled: journaling a
    # snapshot would change the state the next snapshot compares
    assert reg._mutating["state.restore"] is True
    for verb in ("snapshot", "prune", "pin", "unpin", "snapshot_tick"):
        assert reg._mutating[f"state.{verb}"] is False


def test_destructive_verbs_are_not_projected_to_mcp():
    reg = SkillRegistry()
    state_skills.bind(reg)
    for verb in ("snapshot", "restore", "prune"):
        assert "mcp" not in (reg.spec(f"state.{verb}").surfaces or ())
    assert "mcp" in reg.spec("state.ls").surfaces


def test_manifest_declares_the_noun_the_skills_and_the_schedule():
    import tomllib

    path = Path(state_skills.__file__).parents[1] / "axiom-extension.toml"
    m = tomllib.loads(path.read_text())
    prov = m["extension"]["provides"]
    assert any(p["kind"] == "cmd" and p["noun"] == "state" for p in prov)
    declared = {p["name"] for p in prov if p["kind"] == "skill"}
    reg = SkillRegistry()
    state_skills.bind(reg)
    assert declared == set(reg.list("state"))
    sched = m["extension"]["schedule"]
    assert sched[0]["action"] == "state.snapshot_tick" in declared or sched[0]["action"] in declared
    import importlib

    for p in prov:
        if p["kind"] == "skill":
            mod, fn = p["entry"].split(":")
            assert callable(getattr(importlib.import_module(mod), fn))


def test_snapshot_skill_reports_coverage_and_real_next_steps(env):
    ctx = _ctx(env)
    r = ctx.registry.invoke("state.snapshot", {"reason": "t"}, ctx)
    assert r.ok
    v = r.value
    assert any("NOT COVERED" in c and "databases" in c for c in v["coverage"])
    assert any(s.startswith("axi state verify") for s in v["next_steps"])
    again = ctx.registry.invoke("state.snapshot", {}, ctx)
    assert again.value["skipped"] and "state snapshot --force" in " ".join(again.value["next_steps"])


def test_snapshot_skill_disk_pressure_is_a_loud_failure_with_next_steps(env, monkeypatch):
    monkeypatch.setattr(bc, "free_bytes", lambda p: (1, 10**12))
    ctx = _ctx(env)
    r = ctx.registry.invoke("state.snapshot", {}, ctx)
    assert not r.ok and "REFUSING" in r.errors[0]
    assert any("another/disk" in s for s in r.value["next_steps"])


def test_restore_skill_dry_run_then_apply(env):
    ctx = _ctx(env)
    sid = ctx.registry.invoke("state.snapshot", {}, ctx).value["snapshot_id"]
    (env / "settings.toml").write_text("broken")
    dry = ctx.registry.invoke("state.restore", {"id": sid}, ctx)
    assert dry.ok and dry.value["dry_run"] and (env / "settings.toml").read_text() == "broken"
    assert any("--apply" in s for s in dry.value["next_steps"])
    done = ctx.registry.invoke("state.restore", {"id": sid, "apply": True}, ctx)
    assert done.ok and (env / "settings.toml").read_text() == "a = 1\n"
    steps = " ".join(done.value["next_steps"])
    assert "restart" in steps and "undo:" in steps and "pre-restore" in steps


def test_prune_skill_is_dry_run_by_default(env):
    ctx = _ctx(env)
    for i in range(3):
        (env / "settings.toml").write_text(f"a = {i}\n")
        ctx.registry.invoke("state.snapshot", {"force": True, "no_prune": True}, ctx)
        time.sleep(1.01)
    r = ctx.registry.invoke("state.prune", {}, ctx)
    assert r.ok and r.value["applied"] is False and len(ss.Store(env / "snapshots").ids()) == 3


def test_tick_takes_one_then_waits_out_the_interval_then_skips_when_unchanged(env):
    ctx = _ctx(env)
    first = ctx.registry.invoke("state.snapshot_tick", {}, ctx)
    assert first.ok and first.value["snapshot_id"]
    second = ctx.registry.invoke("state.snapshot_tick", {}, ctx)
    assert second.ok and "interval is" in second.actions_taken[0]
    assert len(ss.Store(env / "snapshots").ids()) == 1
    # interval elapsed but nothing changed -> still one snapshot
    (env / "settings.toml").write_text('[state.snapshots]\ninterval_hours = 0.0001\n')
    time.sleep(0.5)
    ctx.registry.invoke("state.snapshot_tick", {"force": True}, ctx)
    n = len(ss.Store(env / "snapshots").ids())
    ctx.registry.invoke("state.snapshot_tick", {}, ctx)
    ctx.registry.invoke("state.snapshot_tick", {}, ctx)
    assert len(ss.Store(env / "snapshots").ids()) == n  # unchanged inputs: no new snapshot


def test_tick_honours_the_disable_switch(env):
    (env / "settings.toml").write_text("[state.snapshots]\nenabled = false\n")
    ctx = _ctx(env)
    r = ctx.registry.invoke("state.snapshot_tick", {}, ctx)
    assert r.ok and "disabled" in r.actions_taken[0] and not (env / "snapshots").exists()


def test_tick_failure_is_loud_and_not_ok(env, monkeypatch, caplog):
    monkeypatch.setattr(bc, "free_bytes", lambda p: (1, 10**12))
    ctx = _ctx(env)
    with caplog.at_level(logging.ERROR):
        r = ctx.registry.invoke("state.snapshot_tick", {}, ctx)
    assert not r.ok and "scheduled state snapshot failed" in r.errors[0]
    assert any("SCHEDULED STATE SNAPSHOT FAILED" in rec.message for rec in caplog.records)


def test_tick_reports_a_corrupt_store(env):
    ctx = _ctx(env)
    ctx.registry.invoke("state.snapshot_tick", {}, ctx)
    store = ss.Store(env / "snapshots")
    store.ledger.write_text("garbage\n")
    (env / "settings.toml").write_text("changed\n")
    r = ctx.registry.invoke("state.snapshot_tick", {"force": True}, ctx)
    assert not r.ok


def test_cli_end_to_end_through_the_dispatch_chokepoint(env, capsys):
    assert state_cli.main(["snapshot"]) == 0
    out = capsys.readouterr().out
    assert "NOT COVERED" in out and "next:" in out
    assert state_cli.main(["ls"]) == 0
    assert "on-demand" in capsys.readouterr().out
    (env / "settings.toml").write_text("oops")
    assert state_cli.main(["restore"]) == 0
    dry = capsys.readouterr().out
    assert "DRY RUN" in dry and (env / "settings.toml").read_text() == "oops"
    assert state_cli.main(["diff"]) == 0
    assert "replace" in capsys.readouterr().out
    assert state_cli.main(["verify"]) == 0
    assert "record this off-box" in capsys.readouterr().out
    assert state_cli.main(["restore", "--apply", "--force"]) == 0
    assert (env / "settings.toml").read_text() == "a = 1\n"
    assert state_cli.main(["prune"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert state_cli.main(["show"]) == 0
    assert state_cli.main(["pin", "latest"]) == 0
    assert state_cli.main(["unpin", "latest"]) == 0


def test_cli_json_and_error_exit_codes(env, capsys):
    assert state_cli.main(["--json", "ls"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert state_cli.main(["restore", "nope"]) == 1
    assert "ERROR" in capsys.readouterr().out
    assert state_cli.main(["verify", "nope"]) == 1


def test_cli_with_db_default_address_is_refused(env, capsys):
    assert state_cli.main(["snapshot", "--with-db", "--db-url", "postgresql://u:p@localhost:5432/x"]) == 1
    assert "default developer database" in capsys.readouterr().out


def test_cli_to_another_directory(env, tmp_path, capsys):
    dest = tmp_path / "disk2"
    assert state_cli.main(["snapshot", "--to", str(dest)]) == 0
    assert (dest / "axi-restore.py").is_file()
    assert state_cli.main(["ls", "--to", str(dest)]) == 0
    assert "on-demand" in capsys.readouterr().out


def test_cli_emergency_runs_the_stdlib_script_isolated(env, tmp_path, capfd):
    assert state_cli.main(["snapshot"]) == 0
    (env / "settings.toml").write_text("oops")
    capfd.readouterr()
    assert state_cli.main(["restore", "--emergency", "--apply"]) == 0
    assert (env / "settings.toml").read_text() == "a = 1\n"
    assert "next steps" in capfd.readouterr().out


def test_cli_with_no_verb_prints_help_and_next_step(capsys):
    assert state_cli.main([]) == 0
    assert "state ls" in capsys.readouterr().out


def test_cli_names_come_from_the_brand_helper(env, monkeypatch, capsys):
    from axiom.infra import branding

    class _B:
        cli_name = "zed"

    monkeypatch.setattr(branding, "get_branding", lambda: _B())
    assert state_cli.main(["snapshot"]) == 0
    out = capsys.readouterr().out
    assert "zed state verify" in out and "axi state" not in out


# --- wiring: risky operations snapshot first -------------------------------


def test_update_snapshots_before_changing_anything_and_not_on_dry_run(monkeypatch, tmp_path):
    from axiom.extensions.builtins.update.cli import Updater

    order = []
    monkeypatch.setattr("axiom.infra.state_snapshots.take_snapshot_before",
                        lambda reason, **kw: order.append(("snapshot", reason)))
    for name in ("_update_deps", "_run_migrations", "_register_agents", "_validate", "_git_pull"):
        monkeypatch.setattr(Updater, name, lambda self, n=name: order.append((n,)))
    monkeypatch.setattr(Updater, "_last_step_failed", lambda self, step: False)
    Updater(repo_root=tmp_path, dry_run=False).update_all(pull=True)
    assert order[0] == ("snapshot", "update --pull") and ("_update_deps",) in order
    order.clear()
    Updater(repo_root=tmp_path, dry_run=True).update_all()
    assert not any(o[0] == "snapshot" for o in order)


def test_secret_rotation_snapshots_before_the_credential_changes(monkeypatch, env):
    from axiom.extensions.builtins.secrets.skills import rotate_foreign as rf

    seen = []
    monkeypatch.setattr("axiom.infra.state_snapshots.take_snapshot_before",
                        lambda reason, **kw: seen.append((reason, kw.get("state_dir"))))

    class _Store:
        def metadata(self, name):
            return {"provider": "guided"}

    class _Decision:
        completed = []
        refused = []
        skipped = []
        halted = False

    monkeypatch.setattr(rf, "guarded_act", lambda *a, **k: seen.append(("guarded",)) or _Decision())
    monkeypatch.setattr(rf, "build_rotation_provider", lambda *a, **k: object())
    ctx = _ctx(env)
    try:
        rf.rotate_foreign("tok", {"_store": _Store()}, ctx)
    except Exception:  # noqa: BLE001 - only the ORDER of the two calls matters here
        pass
    assert seen and seen[0][0] == "secrets rotate tok" and seen[0][1] == env
    assert ("guarded",) in seen and seen.index(("guarded",)) > 0


def test_snapshot_before_never_touches_the_real_state_dir_under_pytest():
    assert os.environ["AXIOM_SNAPSHOT_BEFORE"] == "0"
    assert ss.take_snapshot_before("anything") is None
