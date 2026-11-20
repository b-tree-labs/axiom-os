# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The vault is one component, owned by KEEP, under one noun (ADR-218).

Four promises, each tested here:

* every old ``axi secrets`` verb still works, through an alias that names its
  new form on stderr (table-driven over the full old verb list);
* ``list`` and ``resolve`` read one credential index, so they agree after a
  ``set``, after a redeem and after a ``declare``;
* the index migration is idempotent and lossless;
* the deprecated ``secrets.*`` skills and MCP tools are kept, and say so.

Nothing here touches a real vault: the state directory is a temporary one and
the value store is the 0600 file backend.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.secrets import cli as secrets_cli
from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.extensions.builtins.vault import cli as vault_cli
from axiom.extensions.builtins.vault import index as index_mod
from axiom.extensions.builtins.vault import skills as vault_skills
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult

SENTINEL = "kv-test-SENTINEL-value-0123456789"


@pytest.fixture
def state(tmp_path, monkeypatch):
    from axiom.policy import action_ledger

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    monkeypatch.setenv("AXIOM_MODE", "dev")
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    monkeypatch.delenv(secrets_cli.QUIET_ENV, raising=False)
    action_ledger._reset_backend_cache()
    yield tmp_path
    action_ledger._reset_backend_cache()


def _ctx(state_dir: Path) -> SkillContext:
    return SkillContext(
        registry=vault_skills.bind_default(), state_dir=state_dir,
        logger=logging.getLogger("test.vault"), user_prompt=None,
    )


def _run(name: str, params: dict, state_dir: Path) -> SkillResult:
    from axiom.infra.skill_dispatch import invoke_capability

    ctx = _ctx(state_dir)
    return invoke_capability(ctx.registry, name, params, ctx, surface="cli")


# --------------------------------------------------------------------------- the alias

#: Every old ``axi secrets`` invocation, the ``axi vault`` argv it becomes, the
#: skill that then runs, and parameters that skill must receive. One row per
#: old verb (``vault`` twice: with and without ``setup``).
OLD_VERBS = [
    (["diagnose", "env://X"], ["diagnose", "env://X"], "vault.diagnose", {"ref": "env://X"}),
    (["exposed", "k", "--where", "log"], ["exposed", "k", "--where", "log"], "vault.exposed",
     {"ref": "k", "where": "log"}),
    (["rotate", "k", "--force"], ["rotate", "k", "--force"], "vault.rotate", {"ref": "k", "force": True}),
    (["set", "k", "--issuer-url", "https://h.example"], ["set", "k", "--issuer-url", "https://h.example"],
     "vault.set", {"name": "k", "issuer_url": "https://h.example"}),
    (["list"], ["list"], "vault.list", {}),
    (["rm", "k", "--yes"], ["rm", "k", "--yes"], "vault.rm", {"name": "k", "yes": True}),
    (["get", "k"], ["get", "k"], "vault.get", {"name": "k"}),
    (["audit", "--within-days", "3"], ["audit", "--expiry", "--within-days", "3", "--report-only"],
     "vault.audit", {"within_days": 3, "expiry": True, "report_only": True}),
    (["reindex", "--dry-run"], ["reindex", "--dry-run"], "vault.reindex", {"dry_run": True}),
    (["discover", "--workspace", "/w"], ["discover", "--workspace", "/w"], "vault.discover", {"workspace": "/w"}),
    (["git-credential", "get"], ["git-credential", "get"], "vault.git_credential", {"op": "get"}),
    (["wire-git", "k", "--host", "h"], ["wire-git", "k", "--host", "h"], "vault.wire_git",
     {"name": "k", "host": "h"}),
    (["annotate", "k", "--notes", "n"], ["annotate", "k", "--notes", "n"], "vault.annotate",
     {"name": "k", "notes": "n"}),
    (["vault", "--backend", "openbao"], ["where", "--backend", "openbao"], "vault.where",
     {"backend": "openbao"}),
    (["vault", "setup", "--shell", "zsh", "--yes"], ["setup", "--shell", "zsh", "--yes"], "vault.setup",
     {"shell": "zsh", "yes": True}),
]


def test_the_table_covers_every_old_verb():
    """A verb missing from the table above is a verb nobody checked."""
    covered = {secrets_cli.translate(old)[1] for old, *_ in OLD_VERBS}
    assert covered == set(secrets_cli.VERB_MAP)


@pytest.mark.parametrize(("old", "new", "skill", "params"), OLD_VERBS, ids=lambda v: " ".join(v)
                         if isinstance(v, list) else None)
def test_every_old_command_maps_to_its_vault_form_and_runs_it(
    old, new, skill, params, state, monkeypatch, capsys
):
    translated, old_verb, _ = secrets_cli.translate(old)
    assert translated == new

    seen: dict = {}

    def fake_invoke(registry, name, p, ctx, surface=None):
        seen.update(skill=name, params=dict(p))
        return SkillResult(ok=True, value={})

    monkeypatch.setattr("axiom.infra.skill_dispatch.invoke_capability", fake_invoke)
    monkeypatch.setattr(vault_cli, "_read_value_for_set", lambda: None)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    rc = secrets_cli.main(old)
    err = capsys.readouterr().err
    assert rc == 0
    assert seen["skill"] == skill
    for key, want in params.items():
        assert seen["params"].get(key) == want, (key, seen["params"])
    lines = [ln for ln in err.splitlines() if "deprecated" in ln]
    assert len(lines) == 1, err
    assert f"vault {' '.join(secrets_cli.VERB_MAP[old_verb])}" in lines[0]


def test_the_deprecation_line_can_be_silenced(state, monkeypatch, capsys):
    monkeypatch.setenv(secrets_cli.QUIET_ENV, "1")
    monkeypatch.setattr("axiom.infra.skill_dispatch.invoke_capability",
                        lambda *a, **k: SkillResult(ok=True, value={"count": 0, "items": []}))
    assert secrets_cli.main(["list"]) == 0
    assert "deprecated" not in capsys.readouterr().err


def test_an_old_script_works_end_to_end_through_the_alias(state, monkeypatch, capsys):
    """Not mocked: set, list, get and rm through `secrets`, against a file-backed store."""
    monkeypatch.setattr(vault_cli, "_read_value_for_set", lambda: SENTINEL)
    assert secrets_cli.main(["set", "ci-key", "--issuer-url", "https://git.example.org",
                             "--expires-at", "2099-01-01"]) == 0
    capsys.readouterr()
    assert secrets_cli.main(["--json", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [i["name"] for i in listed["value"]["items"]] == ["ci-key"]
    assert listed["value"]["items"][0]["host"] == "git.example.org"
    assert secrets_cli.main(["get", "ci-key"]) == 0
    out = capsys.readouterr()
    assert SENTINEL not in out.out + out.err
    assert secrets_cli.main(["rm", "ci-key", "--yes"]) == 0
    assert ForeignCredentialStore(state).list() == []


def test_secrets_audit_keeps_its_old_exit_code(state, monkeypatch, capsys):
    """It was a report that never failed; scripts under `set -e` relied on that."""
    ForeignCredentialStore(state).set("undated", SENTINEL.encode())  # no expiry: a finding
    assert secrets_cli.main(["audit"]) == 0
    assert vault_cli.main(["audit", "--expiry"]) == 1, "the new verb is red on a finding"


def test_an_unknown_old_verb_is_rejected_by_the_vault_parser(state, capsys):
    with pytest.raises(SystemExit) as exc:
        secrets_cli.main(["no-such-verb"])
    assert exc.value.code == 2


# --------------------------------------------------------------------------- the noun


def test_help_lists_the_verbs_under_the_four_headings():
    text = vault_cli._parser().format_help()
    positions = [text.index(f"{g} (") for g in ("values", "lifecycle", "records", "setup")]
    assert positions == sorted(positions)
    for group, _, verbs in vault_cli.VERB_GROUPS:
        for verb, _ in verbs:
            assert f"  {verb}" in text, (group, verb)


def test_every_listed_verb_parses():
    parser = vault_cli._parser()
    needs = {"set": ["k"], "get": ["k"], "rm": ["k"], "rotate": ["k"], "exposed": ["k", "--where", "x"],
             "annotate": ["k"], "git-credential": ["get"], "wire-git": ["k", "--host", "h"]}
    for verb in vault_cli.VERBS:
        args = parser.parse_args([verb, *needs.get(verb, [])])
        assert args.subcommand == verb


def test_the_manifest_declares_every_verb_and_the_hidden_alias():
    import tomllib

    manifest = tomllib.loads(
        (Path(vault_cli.__file__).parent / "axiom-extension.toml").read_text(encoding="utf-8")
    )
    cmds = {p["noun"]: p for p in manifest["extension"]["provides"] if p["kind"] == "cmd"}
    assert set(vault_cli.VERBS) <= set(cmds["vault"]["subcommands"])
    assert cmds["secrets"]["tier"] == "internal"
    assert set(cmds["secrets"]["subcommands"]) == {v for v in secrets_cli.VERB_MAP if " " not in v}


def test_there_is_no_separate_secrets_extension():
    """One component: the storage module keeps its import path and has no manifest."""
    from axiom.extensions.builtins import secrets

    assert not (Path(secrets.__file__).parent / "axiom-extension.toml").exists()


# --------------------------------------------------------------------------- one index


def _list_names(state_dir: Path, host: str) -> set[str]:
    result = _run("vault.list", {"host": host}, state_dir)
    assert result.ok, result.errors
    return {i["name"] for i in result.value["items"]}


def _resolve_names(state_dir: Path, host: str) -> set[str]:
    result = _run("vault.resolve", {"host": host}, state_dir)
    return {c["name"] for c in (result.value or {}).get("candidates", [])}


def test_list_and_resolve_agree_after_set(state):
    store = ForeignCredentialStore(state)
    store.set("git-pat", SENTINEL.encode(), git_host="git.example.org", expires_at="2099-01-01")
    store.set("api-key", SENTINEL.encode(), issuer_url="https://git.example.org", notes="api scope")
    store.set("elsewhere", SENTINEL.encode(), issuer_url="https://other.example.org")
    assert _list_names(state, "git.example.org") == _resolve_names(state, "git.example.org") == {
        "git-pat", "api-key"}


def test_list_and_resolve_agree_after_a_redeem(state):
    """The redeem path stores an API key with the node's address as its issuer.

    This is the case that disagreed: the key showed in the list and not in
    resolve. The store call below is the one a consumer's redeem makes.
    """
    ForeignCredentialStore(state).set(
        "node-key", SENTINEL.encode(), provider="axiom-api-key",
        notes="api scope: redeemed from node.example.org", issuer_url="http://node.example.org:8947",
    )
    assert _list_names(state, "node.example.org") == _resolve_names(state, "node.example.org") == {"node-key"}


def test_a_key_with_no_host_is_named_by_resolve_not_silently_missing(state):
    ForeignCredentialStore(state).set("orphan", SENTINEL.encode(), notes="api scope")
    result = _run("vault.resolve", {"host": "node.example.org"}, state)
    assert not result.ok
    assert result.value["without_host"] == ["orphan"]
    assert "annotate" in result.errors[0]
    assert _run("vault.annotate", {"name": "orphan", "host": "node.example.org"}, state).ok
    assert _list_names(state, "node.example.org") == _resolve_names(state, "node.example.org") == {"orphan"}


def test_list_and_resolve_agree_after_declare(state):
    ForeignCredentialStore(state).set("pat", SENTINEL.encode(), git_host="git.example.org")
    declared = _run("vault.declare", {"name": "pat", "disposition": "human_rotatable",
                                      "review_by": "2099-01-01"}, state)
    assert declared.ok, declared.errors
    assert _list_names(state, "git.example.org") == _resolve_names(state, "git.example.org") == {"pat"}


def test_list_resolve_and_audit_report_one_health(state):
    store = ForeignCredentialStore(state)
    store.set("soon", SENTINEL.encode(), git_host="h.example", expires_at="2000-01-01")
    store.set("never", SENTINEL.encode(), git_host="h.example")
    listed = {i["name"]: i["health"] for i in _run("vault.list", {}, state).value["items"]}
    resolved = {c["name"]: c["health"] for c in _run("vault.resolve", {"host": "h.example"}, state)
                .value["candidates"]}
    audited = {f["name"]: f["level"] for f in _run("vault.audit", {"expiry": True}, state)
               .value["expiry"]["all"]}
    assert listed == resolved == audited == {"soon": "expired", "never": "no_expiry"}


def test_a_changed_issuer_moves_the_recorded_host(state):
    store = ForeignCredentialStore(state)
    store.set("k", SENTINEL.encode(), issuer_url="https://old.example.org")
    store.update_metadata("k", issuer_url="https://new.example.org")
    assert _list_names(state, "new.example.org") == _resolve_names(state, "new.example.org") == {"k"}
    assert _list_names(state, "old.example.org") == set()


def test_nothing_returns_a_value(state):
    ForeignCredentialStore(state).set("k", SENTINEL.encode(), git_host="h.example")
    for name, params in (("vault.list", {}), ("vault.resolve", {"host": "h.example"}),
                         ("vault.audit", {})):
        assert SENTINEL not in json.dumps(_run(name, params, state).value, default=str)


# --------------------------------------------------------------------------- the migration

V1 = {
    "version": 1,
    "credentials": {
        "a": {"name": "a", "issuer_url": "https://Git.Example.org/x", "expires_at": "2099-01-01",
              "fingerprint": "abc", "disposition": "self_rotatable", "notes": "api scope"},
        "b": {"name": "b", "git_host": "h.example", "custom_unknown_field": "kept"},
        "c": {"name": "c", "notes": "no host anywhere"},
    },
}


def _write_v1(state_dir: Path) -> Path:
    path = state_dir / "secrets" / "foreign-credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(V1, indent=2), encoding="utf-8")
    return path


def test_the_migration_is_lossless(state):
    path = _write_v1(state)
    original = path.read_text(encoding="utf-8")
    report = index_mod.migrate(state)
    assert report.changed and report.from_version == 1 and report.to_version == 2
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["version"] == 2
    for name, record in V1["credentials"].items():
        for key, value in record.items():
            assert after["credentials"][name][key] == value, (name, key)
    assert after["credentials"]["a"]["host"] == "git.example.org"
    assert after["credentials"]["b"]["host"] == "h.example"
    assert "host" not in after["credentials"]["c"], "no host is invented"
    assert Path(report.backup).read_text(encoding="utf-8") == original, "the v1 file is kept as it was"


def test_the_migration_is_idempotent(state):
    path = _write_v1(state)
    index_mod.migrate(state)
    once = path.read_bytes()
    report = index_mod.migrate(state)
    assert not report.changed
    assert path.read_bytes() == once


def test_a_dry_run_writes_nothing(state):
    path = _write_v1(state)
    before = path.read_bytes()
    report = index_mod.migrate(state, dry_run=True)
    assert report.changed and report.hosts_recorded == ("a", "b")
    assert path.read_bytes() == before
    assert not (path.parent / "foreign-credentials.v1.json").exists()


def test_reindex_runs_the_migration(state):
    path = _write_v1(state)
    result = _run("vault.reindex", {"dry_run": True}, state)
    assert result.value["index_migration"]["changed"] is True
    assert json.loads(path.read_text())["version"] == 1, "a dry run migrates nothing"


def test_any_write_upgrades_a_v1_index_and_keeps_a_copy(state):
    path = _write_v1(state)
    ForeignCredentialStore(state).update_metadata("c", notes="touched")
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["version"] == 2 and after["credentials"]["a"]["host"] == "git.example.org"
    assert json.loads((path.parent / "foreign-credentials.v1.json").read_text()) == V1


def test_a_damaged_index_is_not_migrated(state):
    from axiom.infra.state_integrity import StateDamaged

    path = state / "secrets" / "foreign-credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateDamaged):
        index_mod.migrate(state)
    assert path.read_text(encoding="utf-8") == "{not json"


def test_a_v1_reader_still_finds_the_host(state):
    """A record written by an older release has no `host`; readers derive it."""
    _write_v1(state)
    assert _list_names(state, "git.example.org") == _resolve_names(state, "git.example.org") == {"a"}


# --------------------------------------------------------------------------- audit, one verb


def test_audit_runs_both_sections_by_default(state):
    ForeignCredentialStore(state).set("k", SENTINEL.encode(), expires_at="2099-01-01")
    value = _run("vault.audit", {}, state).value
    assert value["sections"] == ["expiry", "access"]
    assert value["access"]["partial"] is True


def test_audit_access_reads_the_capability_record(state):
    rows = [
        {"ts": 1.0, "tool_name": "secrets.list", "surface": "cli", "principal": "@a:x", "ok": True},
        {"ts": 2.0, "tool_name": "vault.list", "surface": "mcp", "principal": "@h:x", "ok": True},
        {"ts": 3.0, "tool_name": "data.list", "surface": "cli", "ok": True},
    ]
    path = state / "telemetry" / "capabilities.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    access = _run("vault.audit", {"access": True}, state).value["access"]
    (row,) = [r for r in access["verbs"] if r["verb"] == "vault.list"]
    assert row["calls"] >= 2 and row["via_deprecated_name"] >= 1
    assert {"cli", "mcp"} <= set(row["surfaces"])
    assert all(r["verb"].startswith("vault.") for r in access["verbs"])


def test_audit_prints_two_sections(state, capsys):
    ForeignCredentialStore(state).set("k", SENTINEL.encode(), expires_at="2099-01-01")
    vault_cli.main(["audit"])
    out = capsys.readouterr().out
    assert out.index("Expiry") < out.index("Access")


# --------------------------------------------------------------------------- skills and MCP


def test_every_old_skill_name_is_a_deprecated_alias():
    reg = SkillRegistry()
    vault_skills.bind(reg)
    for old, (new, _) in vault_skills.SKILL_ALIASES.items():
        spec, target = reg.spec(old), reg.spec(new)
        assert spec is not None and target is not None, old
        assert spec.description.startswith("DEPRECATED")
        assert vault_skills.MCP_ALIAS_REMOVED_IN in spec.description
        assert set(spec.surfaces or ()) == set(target.surfaces or ()) - {"skill_md"}
        assert spec.risk == target.risk


def test_mcp_offers_the_vault_tools_and_keeps_the_old_names(monkeypatch):
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution

    reg = SkillRegistry()
    vault_skills.bind(reg)
    names = {t.name: t for t in skill_tool_contribution(reg, ctx_factory=lambda: None).tools}
    for verb in ("list", "audit", "diagnose", "discover", "exposed", "rotate", "resolve", "reconcile"):
        assert f"axiom_vault__{verb}" in names
    for verb in ("list", "audit", "diagnose", "discover", "exposed", "rotate"):
        old = names[f"axiom_secrets__{verb}"]
        assert "DEPRECATED" in (old.description or "")
    assert "axiom_secrets__set" not in names and "axiom_vault__set" not in names, "set stays CLI-only"


def test_keep_owns_both_namespaces_through_the_one_extension():
    import tomllib

    manifest = tomllib.loads(
        (Path(vault_cli.__file__).parent / "axiom-extension.toml").read_text(encoding="utf-8")
    )
    (agent,) = [p for p in manifest["extension"]["provides"] if p["kind"] == "agent"]
    assert agent["name"] == "keep"
    assert agent["namespaces"] == ["vault", "secrets"]
    assert "also_owns" not in agent, "KEEP owns one extension now"


def test_the_memory_server_offers_the_vault_names_and_keeps_the_old(monkeypatch):
    from axiom.extensions.builtins.memory import mcp_server

    tools = {t.name: t for t in mcp_server._TOOLS}
    for new, old in (("axiom_vault_list", "axiom_secrets_list"),
                     ("axiom_vault_rotate_trigger", "axiom_secrets_rotate_trigger")):
        assert new in tools and new in mcp_server._HANDLERS
        assert old in mcp_server._HANDLERS
        assert tools[old].description.startswith("DEPRECATED")
