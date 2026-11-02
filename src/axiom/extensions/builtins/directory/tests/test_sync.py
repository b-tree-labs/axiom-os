# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-103 phase 4 — the cadenced projection, its verb, and what survives a restart.

The reconciler existed; nothing ran it. These tests drive the run one
heartbeat performs, from environment to tuples: enumerate → diff → write,
removals recorded in a revoked set that now lives on disk, a dry run that
writes nothing, and the two refusals that keep an outage from becoming a
mass revocation (a provider that cannot enumerate; an enumeration that fails
is never an empty group).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.directory import (
    GroupRef,
    JsonFileRevokedSet,
    JsonFileTupleStore,
    MembershipResolver,
    PrincipalRef,
    RelationTuple,
    run_sync,
)
from axiom.extensions.builtins.directory.config import load_directory_config
from axiom.extensions.builtins.directory.providers import LocalDirectory
from axiom.extensions.builtins.directory.revoked import RevokedSet
from axiom.extensions.builtins.directory.stores import TupleStoreError, resolve_tuple_store
from axiom.infra.skills import SkillContext


def _ctx(tmp_path: Path) -> SkillContext:
    from axiom.extensions.builtins.directory.skills import bind_default

    return SkillContext(
        registry=bind_default(),
        state_dir=tmp_path / "state",
        logger=logging.getLogger("test"),
        user_prompt=None,
    )


def _local(tmp_path: Path, groups: dict) -> Path:
    p = tmp_path / "directory.json"
    p.write_text(json.dumps({"groups": {g: {"members": m} for g, m in groups.items()}}))
    return p


# ---------------------------------------------------------------- run_sync


def test_sync_projects_members_and_records_removals(tmp_path):
    store = JsonFileTupleStore(tmp_path / "tuples.json")
    store.write(
        adds=[
            RelationTuple("user:stale", "member", "group:ops"),
            RelationTuple("user:keep", "member", "group:ops"),
        ]
    )
    provider = LocalDirectory(groups={"ops": ["keep", "new"], "eng": ["e1"]})
    revoked = JsonFileRevokedSet(tmp_path / "revoked.json")
    report = run_sync(
        provider=provider,
        provider_name="local",
        groups=[GroupRef("ops", "local"), GroupRef("eng", "local")],
        store=store,
        revoked=revoked,
    )
    assert report.ok
    assert report.added == 2 and report.removed == 1
    ops = next(g for g in report.groups if g.group_id == "ops")
    assert ops.added == ("new",) and ops.removed == ("stale",) and ops.unchanged == ("keep",)
    assert store.list_members("group:ops", "member") == ["user:keep", "user:new"]
    assert store.list_members("group:eng", "member") == ["user:e1"]
    # the removal bites immediately on resolution, and survives a restart
    assert revoked.is_revoked("stale", "ops")
    again = JsonFileRevokedSet(tmp_path / "revoked.json")
    assert again.is_revoked("stale", "ops")
    resolver = MembershipResolver(role_map=None, revoked=again)
    m = resolver.resolve(PrincipalRef("stale", "local"), claims={"groups": ["ops"]})
    assert m.groups == ()  # token still asserts ops; the revoked set drops it


def test_dry_run_reports_the_diff_and_writes_nothing(tmp_path):
    store = JsonFileTupleStore(tmp_path / "tuples.json")
    store.write(adds=[RelationTuple("user:stale", "member", "group:ops")])
    provider = LocalDirectory(groups={"ops": ["new"]})
    revoked = RevokedSet()
    report = run_sync(
        provider=provider,
        provider_name="local",
        groups=[GroupRef("ops", "local")],
        store=store,
        revoked=revoked,
        dry_run=True,
    )
    assert report.ok and report.dry_run
    assert report.groups[0].added == ("new",) and report.groups[0].removed == ("stale",)
    assert store.list_members("group:ops", "member") == ["user:stale"]  # untouched
    assert len(revoked) == 0


def test_provider_that_cannot_enumerate_is_refused(tmp_path):
    from axiom.extensions.builtins.directory.providers import OidcClaimsDirectory

    store = JsonFileTupleStore(tmp_path / "tuples.json")
    report = run_sync(
        provider=OidcClaimsDirectory(),
        provider_name="oidc_claims",
        groups=[GroupRef("ops", "oidc_claims")],
        store=store,
    )
    assert not report.ok
    assert "cannot enumerate" in report.errors[0]
    assert report.groups == []


def test_failed_enumeration_is_never_an_empty_group(tmp_path):
    store = JsonFileTupleStore(tmp_path / "tuples.json")
    store.write(
        adds=[
            RelationTuple("user:a", "member", "group:ops"),
            RelationTuple("user:b", "member", "group:ops"),
        ]
    )

    class Flaky(LocalDirectory):
        def members_of(self, ref):
            if ref.id == "ops":
                raise ConnectionError("directory unreachable")
            return super().members_of(ref)

    provider = Flaky(groups={"ops": [], "eng": ["e1"]})
    report = run_sync(
        provider=provider,
        provider_name="local",
        groups=[GroupRef("ops", "local"), GroupRef("eng", "local")],
        store=store,
    )
    assert not report.ok
    ops = next(g for g in report.groups if g.group_id == "ops")
    assert ops.error and "enumeration failed" in ops.error
    assert ops.removed == ()
    # nobody was removed from ops; eng still synced
    assert store.list_members("group:ops", "member") == ["user:a", "user:b"]
    assert store.list_members("group:eng", "member") == ["user:e1"]


def test_missing_store_or_groups_is_reported(tmp_path):
    provider = LocalDirectory(groups={"ops": ["a"]})
    r = run_sync(
        provider=provider, provider_name="local", groups=[GroupRef("ops", "local")], store=None
    )
    assert "no tuple store" in r.errors[0]
    r = run_sync(
        provider=provider,
        provider_name="local",
        groups=[],
        store=JsonFileTupleStore(tmp_path / "t.json"),
    )
    assert "no groups" in r.errors[0]


# ---------------------------------------------------------------- stores


def test_json_tuple_store_refuses_duplicate_add_and_missing_delete(tmp_path):
    store = JsonFileTupleStore(tmp_path / "t.json")
    t = RelationTuple("user:a", "member", "group:g")
    store.write(adds=[t])
    with pytest.raises(TupleStoreError):
        store.write(adds=[t])
    with pytest.raises(TupleStoreError):
        store.write(deletes=[RelationTuple("user:zz", "member", "group:g")])
    assert oct((tmp_path / "t.json").stat().st_mode & 0o777) == "0o600"
    assert store.all_tuples() == [("user:a", "member", "group:g")]


def test_resolve_tuple_store_from_env(tmp_path):
    assert resolve_tuple_store({}) is None
    s = resolve_tuple_store({"AXIOM_DIRECTORY_TUPLE_STORE": f"json:{tmp_path / 'x.json'}"})
    assert isinstance(s, JsonFileTupleStore)
    with pytest.raises(ValueError):
        resolve_tuple_store({"AXIOM_DIRECTORY_TUPLE_STORE": "json:"})
    with pytest.raises(ValueError):
        resolve_tuple_store({"AXIOM_DIRECTORY_TUPLE_STORE": "redis"})


# ---------------------------------------------------------------- revoked file


def test_persisted_revoked_set_expires_and_clears(tmp_path):
    p = tmp_path / "revoked.json"
    r = JsonFileRevokedSet(p, window=100)
    r.record("s", "g", now=1000)
    assert json.loads(p.read_text())["entries"] == [["s", "g", 1000]]
    assert JsonFileRevokedSet(p, window=100).is_revoked("s", "g", now=1050)
    assert not JsonFileRevokedSet(p, window=100).is_revoked("s", "g", now=1200)  # expired
    r.clear_entry("s", "g")
    assert json.loads(p.read_text())["entries"] == []
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_persisted_revoked_set_tolerates_a_broken_file(tmp_path):
    p = tmp_path / "revoked.json"
    p.write_text("{not json")
    r = JsonFileRevokedSet(p)
    assert len(r) == 0
    r.record("s", "g")
    assert json.loads(p.read_text())["entries"][0][:2] == ["s", "g"]


# ---------------------------------------------------------------- config


def test_config_local_provider_defaults_groups_from_role_map_then_file(tmp_path):
    d = _local(tmp_path, {"grp-ops": ["a"], "grp-x": ["b"]})
    roles = tmp_path / "roles.json"
    roles.write_text(
        '{"rules": [{"group": "grp-ops", "role": "operator"}, {"group": "grp-*", "role": "*"}]}'
    )
    cfg = load_directory_config(
        {
            "AXIOM_DIRECTORY_PROVIDER": "local",
            "AXIOM_DIRECTORY_LOCAL_FILE": str(d),
            "AXIOM_DIRECTORY_ROLE_MAP": str(roles),
            "AXIOM_DIRECTORY_TUPLE_STORE": f"json:{tmp_path / 't.json'}",
        },
        state_dir=tmp_path,
    )
    assert cfg.errors == []
    assert cfg.can_enumerate
    assert [g.id for g in cfg.sync_groups] == ["grp-ops"]  # literal groups only, not grp-*
    assert cfg.revoked_path == tmp_path / "directory" / "revoked.json"
    # no role map → every group in the local file
    cfg2 = load_directory_config(
        {"AXIOM_DIRECTORY_PROVIDER": "local", "AXIOM_DIRECTORY_LOCAL_FILE": str(d)},
        state_dir=tmp_path,
    )
    assert [g.id for g in cfg2.sync_groups] == ["grp-ops", "grp-x"]
    # explicit list wins
    cfg3 = load_directory_config(
        {
            "AXIOM_DIRECTORY_PROVIDER": "local",
            "AXIOM_DIRECTORY_LOCAL_FILE": str(d),
            "AXIOM_DIRECTORY_SYNC_GROUPS": "grp-x",
        },
        state_dir=tmp_path,
    )
    assert [g.id for g in cfg3.sync_groups] == ["grp-x"]


def test_config_reports_misconfiguration_instead_of_raising(tmp_path):
    cfg = load_directory_config({"AXIOM_DIRECTORY_PROVIDER": "local"}, state_dir=tmp_path)
    assert any("AXIOM_DIRECTORY_LOCAL_FILE" in e for e in cfg.errors)
    cfg = load_directory_config({"AXIOM_DIRECTORY_PROVIDER": "entra"}, state_dir=tmp_path)
    assert any("provider=entra needs" in e for e in cfg.errors)
    cfg = load_directory_config({"AXIOM_DIRECTORY_PROVIDER": "ldap"}, state_dir=tmp_path)
    assert any("unknown" in e for e in cfg.errors)
    cfg = load_directory_config({}, state_dir=tmp_path)
    assert cfg.provider_name == "oidc_claims" and not cfg.can_enumerate and cfg.errors == []


def test_config_entra_with_injected_graph_client_enumerates(tmp_path):
    class Graph:
        def get(self, path):
            assert path.startswith("/groups/g1/members")
            return {"value": [{"id": "oid-1"}, {"id": "oid-2"}]}

    cfg = load_directory_config(
        {"AXIOM_DIRECTORY_PROVIDER": "entra", "AXIOM_DIRECTORY_SYNC_GROUPS": "g1"},
        state_dir=tmp_path,
        graph_client=Graph(),
    )
    assert cfg.errors == [] and cfg.can_enumerate
    assert [p.subject for p in cfg.provider.members_of(GroupRef("g1", "entra"))] == [
        "oid-1",
        "oid-2",
    ]


def test_graph_adapter_strips_absolute_next_links():
    from axiom.extensions.builtins.directory.config import _GraphGet

    class Vendor:
        base = "https://graph.microsoft.com/v1.0"

        def request(self, method, path, **kw):
            assert method == "GET" and path == "/groups/g/members?$skiptoken=x"
            return {"value": []}

    assert _GraphGet(Vendor()).get(
        "https://graph.microsoft.com/v1.0/groups/g/members?$skiptoken=x"
    ) == {"value": []}


# ---------------------------------------------------------------- skills + cli


def test_sync_skill_end_to_end_writes_last_sync(tmp_path):
    from axiom.extensions.builtins.directory.skills import status, sync

    d = _local(tmp_path, {"ops": ["a", "b"]})
    env = {
        "AXIOM_DIRECTORY_PROVIDER": "local",
        "AXIOM_DIRECTORY_LOCAL_FILE": str(d),
        "AXIOM_DIRECTORY_TUPLE_STORE": f"json:{tmp_path / 't.json'}",
    }
    ctx = _ctx(tmp_path)
    r = sync({"env": env}, ctx)
    assert r.ok, r.errors
    assert r.value["added"] == 2 and r.actions_taken == ["ops: +2 -0"]
    assert (Path(ctx.state_dir) / "directory" / "last-sync.json").is_file()
    s = status({"env": env}, ctx)
    assert s.ok and s.value["last_sync"]["added"] == 2
    assert s.value["last_sync_age_s"] is not None
    assert s.value["config"]["tuple_store"].startswith("json:")
    # second run is a no-op with no actions
    r2 = sync({"env": env}, ctx)
    assert r2.ok and r2.actions_taken == [] and r2.value["added"] == 0


def test_sync_skill_refuses_on_config_errors_and_group_override(tmp_path):
    from axiom.extensions.builtins.directory.skills import sync

    ctx = _ctx(tmp_path)
    r = sync({"env": {"AXIOM_DIRECTORY_PROVIDER": "local"}}, ctx)
    assert not r.ok and "AXIOM_DIRECTORY_LOCAL_FILE" in r.errors[0]
    d = _local(tmp_path, {"ops": ["a"], "eng": ["e"]})
    env = {
        "AXIOM_DIRECTORY_PROVIDER": "local",
        "AXIOM_DIRECTORY_LOCAL_FILE": str(d),
        "AXIOM_DIRECTORY_TUPLE_STORE": f"json:{tmp_path / 't.json'}",
    }
    r = sync({"env": env, "groups": ["eng"]}, ctx)
    assert r.ok and [g["group_id"] for g in r.value["groups"]] == ["eng"]


def test_cli_sync_and_status(tmp_path, monkeypatch, capsys):
    from axiom.extensions.builtins.directory import cli

    d = _local(tmp_path, {"ops": ["a"]})
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_DIRECTORY_PROVIDER", "local")
    monkeypatch.setenv("AXIOM_DIRECTORY_LOCAL_FILE", str(d))
    monkeypatch.setenv("AXIOM_DIRECTORY_TUPLE_STORE", f"json:{tmp_path / 't.json'}")
    assert cli.main(["sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "ops: +1 -0" in out
    assert cli.main(["sync", "--heartbeat"]) == 0
    capsys.readouterr()
    assert cli.main(["status", "--json"]) == 0
    status_out = json.loads(capsys.readouterr().out)
    assert status_out["config"]["provider"] == "local"
    assert status_out["last_sync"]["ok"] is True
    # heartbeat exit code 2 on a failing group
    monkeypatch.setenv("AXIOM_DIRECTORY_PROVIDER", "oidc_claims")
    assert cli.main(["sync", "--heartbeat"]) == 2


def test_manifest_declares_the_heartbeat_and_cmd():
    import axiom.extensions.builtins.directory as pkg
    from axiom.extensions.contracts import parse_manifest

    ext = parse_manifest(Path(pkg.__file__).parent / "axiom-extension.toml")
    assert ext.agent is not None and ext.agent.is_registrable
    assert ext.agent.heartbeat_command == "directory sync --heartbeat"
    assert ext.agent.heartbeat_interval == 900
    import tomllib

    raw = tomllib.loads((Path(pkg.__file__).parent / "axiom-extension.toml").read_text())
    cmds = [p for p in raw["extension"]["provides"] if p.get("kind") == "cmd"]
    assert [c["noun"] for c in cmds] == ["directory"]
    assert set(cmds[0]["subcommands"]) == {"sync", "status"}
