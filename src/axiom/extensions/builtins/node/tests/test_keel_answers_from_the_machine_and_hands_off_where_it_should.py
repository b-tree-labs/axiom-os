"""KEEL's skills answer from collectors a test can supply, and hand off where the table says.

The transcripts stand in for a node with a vLLM engine under a separate
account (TLS, key on the command line), Ollama on loopback, a hot tier under
its floor and a declared bulk tier. Every assertion is about what KEEL says
and whom it hands to; none reads the real machine.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.node import collect, declarations
from axiom.extensions.builtins.node.commands import cli
from axiom.extensions.builtins.node.skills import SPECS, bind
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

PS = """\
12058 llmacct 1344156 /opt/vllm/bin/vllm serve org/Model-27B --served-model-name big qwen --api-key REDACTED --ssl-certfile /x.crt --host 0.0.0.0 --port 41883
973795 ollama 960188 /usr/local/bin/ollama serve
1793988 svc 10 /opt/litellm/bin/litellm --config /etc/serving/litellm.yaml --port 8700
"""
UNITS_USER = "axiom-serve.service loaded active running Axiom composed serving app\nadopt-x.service loaded failed failed Old copy\n"
UNITS_SYS = "vllm-server.service loaded active running vLLM server\nollama.service loaded active running Ollama\n"
SS = 'LISTEN 0 1 0.0.0.0:41883 0.0.0.0:* users:(("vllm",pid=12058,fd=1))\nLISTEN 0 1 127.0.0.1:11434 0.0.0.0:* users:(("ollama",pid=973795,fd=4))\n'
GPUS = "0, GPU-A, 94699, 97887\n"
APPS = "22566, VLLM::EngineCore, 93690\n"
DF = (
    "Filesystem 1-blocks Used Available Capacity Mounted on\n"
    "/dev/root 264000000000 202000000000 50000000000 81% /\n"
    "/dev/home 3800000000000 3740000000000 60000000000 98% /home\n"
    "/dev/bulk 20000000000000 15000000000000 3800000000000 81% /bulk\n"
)


def _models(port, host, tls):
    if port == 41883:
        return {"status": "key required", "path": "/v1/models", "models": []}
    if port == 11434:
        return {
            "status": "ok",
            "path": "/api/tags",
            "models": ["gemma2:2b", "nomic-embed-text:latest", "gemma3:4b"],
        }
    return {"status": "unreachable", "path": "", "models": []}


@pytest.fixture
def collectors():
    return collect.Collectors(
        ps=lambda: PS,
        user_units=lambda: UNITS_USER,
        system_units=lambda: UNITS_SYS,
        listeners=lambda: SS,
        gpus=lambda: GPUS,
        gpu_apps=lambda: APPS,
        df=lambda: DF,
        models=_models,
        extra={"docker_df": ""},
    )


@pytest.fixture
def ctx(tmp_path, collectors):
    reg = SkillRegistry()
    bind(reg)
    declarations.declare(tmp_path, "tier", mount="/home", tier="hot", floor_gb=150)
    declarations.declare(tmp_path, "tier", mount="/bulk", tier="bulk")
    declarations.declare(
        tmp_path,
        "engine",
        name="qwen-vllm",
        engine_kind="vllm",
        unit="vllm-server.service",
        port=41883,
        weights="/opt/weights",
        owner="llm-account",
    )
    with collect.use(collectors):
        yield SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))


def test_the_manifest_and_the_specs_declare_the_same_skills():
    import tomllib

    manifest = tomllib.loads(
        (Path(cli.__file__).parent.parent / "axiom-extension.toml").read_text()
    )
    declared = {p["name"] for p in manifest["extension"]["provides"] if p["kind"] == "skill"}
    assert declared == {s.name for s in SPECS}
    agent = next(p for p in manifest["extension"]["provides"] if p["kind"] == "agent")
    assert agent["name"] == "keel" and set(agent["classes"]) == {"steward", "sensor"}
    assert manifest["agent"]["default_consent"] == "core"


def test_inventory_names_the_declared_engine_sees_the_exposed_key_and_reads_models(ctx):
    r = ctx.registry.invoke("node.inventory", {}, ctx)
    assert r.ok
    engines = {e["name"]: e for e in r.value["engines"]}
    v = engines["qwen-vllm"]
    assert (
        v["source"] == "declared"
        and v["running"]
        and v["owner"] == "llm-account"
        and v["port"] == 41883
    )
    assert v["probe"] == "key required" and "command line" in v["credential"]
    assert engines["ollama"]["declared"] is False
    served = {m["name"] for m in r.value["models"]}
    assert {"big", "qwen", "gemma2:2b", "nomic-embed-text:latest"} <= served
    assert r.value["exposed_credentials"][0]["next"] == "vault.exposed"
    disks = {d["mount"]: d for d in r.value["disks"]}
    assert disks["/home"]["tier"] == "hot" and disks["/bulk"]["tier"] == "bulk"
    targets = [s["target"] for s in r.next]
    assert "node.drift" in targets and "node.declare" in targets and "vault.exposed" in targets


def test_vitals_judges_the_hot_tier_against_its_floor_and_hands_a_breach_to_herald(ctx):
    alerts: list[dict] = []
    ctx.registry.register_skill(
        SkillSpec(
            name="notifications.alert",
            fn=lambda p, c: (alerts.append(p), SkillResult(ok=True, value={"posted": True}))[1],
            description="stub",
            surfaces=("cli",),
            side_effects=True,
            risk="reversible-write",
        ),
        mutating=True,
    )
    r = ctx.registry.invoke("node.vitals", {"alert": True, "thresholds": True}, ctx)
    assert r.ok and r.value["verdict"] == "critical"  # 56 GB free under half of the 150 GB floor
    home = next(d for d in r.value["disks"] if d["mount"] == "/home")
    assert home["state"] == "critical" and home["floor_gb"] == 150
    assert (
        alerts
        and alerts[0]["actor"] == "@keel:local"
        and "critical" in alerts[0]["summary"]
        and alerts[0]["priority"] == "high"
    )
    assert r.value["alert"].startswith("notifications.alert: ok")
    assert {s["target"] for s in r.next} >= {"node.clean", "data.storage"}
    assert r.value["thresholds"]["declared"][0]["mount"] in ("/home", "/bulk")


def test_vitals_says_when_herald_is_not_reachable_instead_of_pretending(ctx):
    r = ctx.registry.invoke("node.vitals", {"alert": True}, ctx)
    assert r.ok and r.value["alert"].startswith("notifications.alert: unreachable")
    assert any("could not hand the breach" in a for a in r.actions_taken)


def test_drift_names_the_credential_on_argv_the_hand_edits_and_the_unknown_unit(
    ctx, tmp_path, monkeypatch
):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "default.conf").write_text("live")
    (cfg / "default.conf.bak-1").write_text("old")
    (cfg / "default.conf.bak-2").write_text("older")
    d = declarations.load(tmp_path)
    d.config_dirs = [str(cfg)]
    declarations.save(tmp_path, d)
    declarations.declare(tmp_path, "engine", name="ghost", unit="ghost.service")
    monkeypatch.setattr(
        "axiom.extensions.builtins.node.skills.drift.pins_rule", lambda settings=None: []
    )
    r = ctx.registry.invoke("node.drift", {}, ctx)
    assert r.ok
    codes = {f["code"] for f in r.value["findings"]}
    assert {"credential-on-argv", "hand-edited-untracked", "declared-unit-unknown"} <= codes
    hand = next(f for f in r.value["findings"] if f["code"] == "hand-edited-untracked")
    assert "2 backup copies" in hand["message"] and "node declare tracked" in hand["fix"]
    assert {s["target"] for s in r.next} >= {"node.declare", "vault.exposed"}


def test_a_tracked_file_that_matches_its_source_is_quiet_and_one_that_differs_is_named(
    ctx, tmp_path, monkeypatch
):
    live, src = tmp_path / "live.conf", tmp_path / "src.conf"
    live.write_text("same")
    src.write_text("same")
    declarations.declare(tmp_path, "tracked", live=str(live), source=str(src))
    d = declarations.load(tmp_path)
    d.config_dirs = [str(tmp_path / "nothing-here")]
    declarations.save(tmp_path, d)
    monkeypatch.setattr(
        "axiom.extensions.builtins.node.skills.drift.pins_rule", lambda settings=None: []
    )
    r = ctx.registry.invoke("node.drift", {"only": ["files"]}, ctx)
    assert r.value["findings"] == []
    src.write_text("changed upstream")
    r = ctx.registry.invoke("node.drift", {"only": ["files"]}, ctx)
    assert [f["code"] for f in r.value["findings"]] == ["tracked-differs"]


def test_declare_writes_durably_and_refuses_nonsense(ctx, tmp_path):
    r = ctx.registry.invoke("node.declare", {"kind": "tier", "mount": "/data", "tier": "bulk"}, ctx)
    assert (
        r.ok
        and r.value["what"] == "/data declared as the bulk tier"
        and r.actions_taken == [r.value["what"]]
    )
    assert any(t.mount == "/data" for t in declarations.load(tmp_path).tiers)
    bad = ctx.registry.invoke("node.declare", {"kind": "tier", "mount": "/x", "tier": "warm"}, ctx)
    assert not bad.ok and "hot, bulk" in bad.errors[0]


def test_clean_plans_unpinned_models_and_an_archive_but_never_applies(ctx, monkeypatch):
    from axiom.llm import model_alignment as ma

    monkeypatch.setattr(
        ma,
        "check",
        lambda **_: ma.Alignment(
            pins={"quick": "gemma2:2b", "reasoning": "qwen2.5:7b", "embed": "nomic-embed-text"},
            ensured={},
            declared=[],
            installed={"gemma2:2b", "nomic-embed-text", "gemma3:4b"},
        ),
    )
    r = ctx.registry.invoke("node.clean", {"plan": True}, ctx)
    assert r.ok
    actions = {(i["action"], i["what"]) for i in r.value["items"]}
    assert ("unpin", "ollama model gemma3:4b") in actions
    assert any(a == "archive" and "/home" in w for a, w in actions)
    assert r.value["plan_id"] and "nothing measured is ever deleted" in r.value["note"]
    denied = ctx.registry.invoke("node.clean", {"apply": r.value["plan_id"]}, ctx)
    assert not denied.ok and "not available in this release" in denied.errors[0]


def test_report_composes_everything_and_renders_markdown(ctx, monkeypatch):
    monkeypatch.setattr(
        "axiom.extensions.builtins.node.skills.drift.pins_rule", lambda settings=None: []
    )
    monkeypatch.setattr(
        "axiom.extensions.builtins.node.skills.doctor.gather",
        lambda check=None: {
            "findings": [
                {
                    "name": "X",
                    "category": "c",
                    "severity": "warning",
                    "message": "x is off",
                    "fix": "turn it on",
                    "detail": None,
                }
            ],
            "summary": "1 check(s) need attention: X",
            "errors": 0,
            "warnings": 1,
        },
    )
    r = ctx.registry.invoke("node.report", {"markdown": True}, ctx)
    assert r.ok
    md = r.value["markdown"]
    assert (
        md.startswith("# Node report")
        and "## Vitals" in md
        and "qwen-vllm" in md
        and "[warning] X: x is off Fix: turn it on" in md
    )
    assert r.next[0]["target"] == "fleet.report"


def test_the_cli_is_a_thin_wrapper_that_prints_json(ctx, monkeypatch, capsys):
    monkeypatch.setattr(
        "axiom.extensions.builtins.node.commands.cli.SkillContext", lambda **kw: ctx
    )
    rc = cli.main(["--json", "inventory", "--only", "disks"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [d["mount"] for d in out["disks"]] == ["/", "/home", "/bulk"]
    assert "engines" not in out
