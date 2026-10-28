# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi data connector add box`` against a fake Box, end to end.

Each test drives the real setup path (token exchange, folder walk, one file's
metadata, dry-run routing, vault write, registration) against an in-process Box
that answers with Box's own error shapes. No call leaves the machine.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.agents.plinth.connectors import load_connector
from axiom.extensions.builtins.data_platform.skills import connector as con
from axiom.extensions.builtins.secrets.providers.protocol import Capabilities, Secret, SecretRef
from axiom.infra.skills import SkillContext, SkillRegistry

from .fake_box import FakeBox, FakeBoxServer


class _MemoryVault:
    """The vault's value store, in memory: whatever lands here never lands on disk."""

    capabilities = Capabilities(read=True, write=True, delete=True)

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def get(self, ref: SecretRef) -> Secret:
        if ref.path not in self.values:
            raise KeyError(ref.path)
        return Secret(value=self.values[ref.path], metadata={"backend": "memory"})

    def put(self, ref: SecretRef, value: bytes) -> None:
        self.values[ref.path] = value

    def delete(self, ref: SecretRef) -> None:
        self.values.pop(ref.path)

    def list_paths(self, prefix: str) -> list[str]:
        return sorted(p for p in self.values if p.startswith(prefix))


@pytest.fixture
def box(monkeypatch):
    world = FakeBox()
    world.add_folder("1001", "Instrument drops")
    world.add_folder("1002", "Run 7", parent="1001", shared=False)
    world.add_file("2001", "run-0007.csv", parent="1002", size=2048)
    world.add_file("2002", "run-0007.png", parent="1002", size=4096)
    world.add_file("2003", "notes.txt", parent="1001", size=10)
    world.add_folder("1003", "Not shared", shared=False)
    world.add_file("2004", "hidden.csv", parent="1003")
    world.add_folder("1004", "Empty drop")
    with FakeBoxServer(world) as server:
        from axiom.extensions.builtins.data_platform.sources.box import ccg_auth, session_api

        monkeypatch.setattr(ccg_auth, "_TOKEN_URL", server.url + "/oauth2/token")
        monkeypatch.setattr(session_api, "_BOX_API_BASE", server.url + "/2.0")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
                    "BOX_DEVELOPER_TOKEN", "AXIOM_SERVED_SITES"):
            monkeypatch.delenv(var, raising=False)
        yield world


@pytest.fixture
def vault(monkeypatch):
    store = _MemoryVault()
    from axiom.extensions.builtins.secrets.foreign import store as foreign

    monkeypatch.setattr(foreign, "open_default_value_store", lambda _state_dir: store)
    return store


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    return SkillContext(registry=SkillRegistry(), state_dir=state, logger=logging.getLogger("t"))


def _add(box: FakeBox, ctx, **over):
    params = {"kind": "box", "site": "site-a", "client_id": box.client_id,
              "client_secret": box.client_secret, "enterprise_id": box.enterprise_id,
              "folder_id": "1001", **over}
    return con.add({k: v for k, v in params.items() if v is not None}, ctx)


def _registered(ctx, name="site-a-box") -> bool:
    return (Path(ctx.state_dir) / "plinth" / "connectors" / f"{name}.toml").exists()


# ---------------------------------------------------------------- happy path


def test_add_verifies_live_then_stores_the_app_in_the_vault_and_registers_a_reference(box, vault, ctx):
    result = _add(box, ctx)

    assert result.ok, result.errors
    v = result.value
    assert [s["name"] for s in v["steps"]] == ["Sign in to Box", "Open the folder", "Read one file", "Dry-run pull"]
    assert v["account"] == box.login
    assert v["sample_file"]["sha1"]
    cfg = load_connector("site-a-box", state_dir=ctx.state_dir)
    assert cfg.site == "site-a" and cfg.params["folder_id"] == "1001"
    assert cfg.params["jwt_secret_ref"] == "foreign://box-app-site-a-box"
    stored = json.loads(vault.values["box-app-site-a-box"])
    assert stored == {"client_id": box.client_id, "client_secret": box.client_secret,
                      "enterprise_id": box.enterprise_id}
    assert '[connector.site-a-box]\nsite = "site-a"' in v["declaration"]


def test_the_dry_run_routes_every_file_and_fetches_and_writes_nothing(box, vault, ctx):
    result = _add(box, ctx, default_disposition="allow", exclude_ext=["png"])

    assert result.ok, result.errors
    rows = result.value["dry_run"]["would_land"]
    assert sorted(r["path"] for r in rows) == ["/Instrument drops/Run 7/run-0007.csv", "/Instrument drops/notes.txt"]
    assert {r["disposition"] for r in rows} == {"allow"}
    assert all("/site-a-box/_records/" in r["lands_at"] for r in rows)
    assert not any(r.endswith("/content") for r in box.requests), "a dry run fetched file content"
    bronze = Path(ctx.state_dir) / "bronze"
    assert not bronze.exists() or not any(bronze.rglob("*")), "a dry run wrote to bronze"


def test_without_a_folder_id_it_lists_what_the_app_can_see_and_lets_the_person_pick(box, vault, ctx):
    asked: list[str] = []
    answers = iter(["2"])  # the second folder listed

    def ask(q):
        asked.append(q)
        return next(answers)

    said: list[str] = []
    result = con.add({"kind": "box", "site": "site-a", "client_id": box.client_id,
                      "enterprise_id": box.enterprise_id, "_ask": ask,
                      "_ask_secret": lambda _q: box.client_secret, "_say": said.append}, ctx)

    assert result.ok, result.errors
    listing = "\n".join(said)
    assert "Instrument drops" in listing and "Empty drop" in listing
    assert "Not shared" not in listing, "listed a folder the app cannot see"
    assert result.value["folder"]["id"] == "1004"
    assert asked == ["Pick a folder [1-2] or paste a folder id: "]


def test_it_asks_only_for_what_it_cannot_detect(box, vault, ctx, monkeypatch):
    monkeypatch.setenv("AXIOM_SERVED_SITES", "site-a")
    asked: list[str] = []

    def ask(q):
        asked.append(q)
        return {"Box app Client ID: ": box.client_id, "Box Enterprise ID: ": box.enterprise_id}[q]

    result = con.add({"kind": "box", "folder_id": "1001", "_ask": ask,
                      "_ask_secret": lambda _q: box.client_secret}, ctx)

    assert result.ok, result.errors
    assert result.value["site"] == "site-a", "the node serves one site; it should not be asked"
    assert asked == ["Box app Client ID: ", "Box Enterprise ID: "]


# ---------------------------------------------------------------- failures name cause and fix


def test_a_wrong_secret_is_named_and_nothing_is_stored_or_registered(box, vault, ctx):
    result = _add(box, ctx, client_secret="not-the-secret")

    assert not result.ok
    assert "client id or client secret" in result.value["cause"]
    assert "OAuth 2.0 Credentials" in result.value["fix"]
    assert vault.values == {}, "a secret that failed sign-in was stored"
    assert not _registered(ctx)


def test_a_wrong_enterprise_id_is_named(box, vault, ctx):
    result = _add(box, ctx, enterprise_id="999")

    assert not result.ok
    assert "enterprise id" in result.value["cause"]
    assert "Platform Apps Manager" in result.value["fix"]


def test_an_app_no_admin_authorized_is_named_as_an_admin_fix(box, vault, ctx):
    box.authorized = False
    result = _add(box, ctx)

    assert not result.ok
    assert "not allowed to sign in" in result.value["cause"]
    assert result.value["steps"][0]["who"] == "admin"


def test_a_folder_not_shared_with_the_app_names_the_account_to_share_it_with(box, vault, ctx):
    result = _add(box, ctx, folder_id="1003")

    assert not result.ok
    assert "cannot see folder 1003" in result.value["cause"]
    assert box.login in result.value["fix"]
    assert vault.values == {}
    assert not _registered(ctx)


def test_an_app_that_sees_no_folder_says_to_share_one(box, vault, ctx):
    for f in box.folders.values():
        f["shared"] = False
    result = con.add({"kind": "box", "site": "site-a", "client_id": box.client_id,
                      "client_secret": box.client_secret, "enterprise_id": box.enterprise_id,
                      "_ask": lambda _q: "1"}, ctx)

    assert not result.ok
    assert "can see no folders" in result.value["cause"]
    assert box.login in result.value["fix"]


def test_an_empty_folder_registers_and_says_nothing_will_land_yet(box, vault, ctx):
    result = _add(box, ctx, folder_id="1004")

    assert result.ok, result.errors
    assert result.value["sample_file"] is None
    assert "no files yet" in result.value["steps"][2]["detail"]
    assert result.value["dry_run"]["totals"]["files"] == 0


def test_an_unreachable_box_is_named_as_the_network(box, vault, ctx, monkeypatch):
    from axiom.extensions.builtins.data_platform.sources.box import ccg_auth

    monkeypatch.setattr(ccg_auth, "_TOKEN_URL", "http://127.0.0.1:9/oauth2/token")  # discard port: refused
    result = _add(box, ctx)

    assert not result.ok
    assert result.value["cause"].startswith("network:")
    assert "api.box.com" in result.value["fix"]


def test_no_vault_stops_before_the_secret_is_asked_for(box, ctx, monkeypatch):
    from axiom.extensions.builtins.secrets.foreign import store as foreign

    def no_vault(_state_dir):
        raise RuntimeError("no OS keychain available")

    monkeypatch.setattr(foreign, "open_default_value_store", no_vault)
    asked: list[str] = []
    result = con.add({"kind": "box", "site": "site-a", "_ask": asked.append,
                      "_ask_secret": asked.append}, ctx)

    assert not result.ok
    assert "no vault" in result.value["cause"]
    assert "secrets vault" in result.value["fix"]
    assert asked == [], "asked for the app's values with nowhere to keep them"


def test_non_interactive_names_the_missing_flag(box, vault, ctx):
    result = con.add({"kind": "box", "site": "site-a", "client_id": box.client_id,
                      "enterprise_id": box.enterprise_id}, ctx)
    assert not result.ok
    assert "--client-secret-stdin" in result.value["fix"]

    result = _add(box, ctx, folder_id=None)
    assert not result.ok
    assert "--folder-id" in result.value["fix"] and "1001 Instrument drops" in result.value["fix"]


def test_an_existing_connector_is_not_replaced_without_force(box, vault, ctx):
    assert _add(box, ctx).ok
    again = _add(box, ctx, folder_id="1004")
    assert not again.ok and "already registered" in again.value["cause"]
    assert _add(box, ctx, folder_id="1004", force=True).ok
    assert load_connector("site-a-box", state_dir=ctx.state_dir).params["folder_id"] == "1004"


# ---------------------------------------------------------------- test / --check / list / show / remove


def test_test_reverifies_through_the_vault_reference_the_pull_uses(box, vault, ctx):
    assert _add(box, ctx).ok

    ok = con.test({"name": "site-a-box"}, ctx)
    assert ok.ok, ok.errors
    assert ok.value["dry_run"]["totals"]["files"] == 3

    box.folders["1001"]["shared"] = False  # somebody unshared the folder
    bad = con.test({"name": "site-a-box"}, ctx)
    assert not bad.ok
    assert box.login in bad.errors[0]

    check = _add(box, ctx, check=True)
    assert not check.ok, "--check is the same verification as test"


def test_list_and_show_never_print_a_secret(box, vault, ctx):
    assert _add(box, ctx).ok

    listed = con.list_all({}, ctx)
    shown = con.show({"name": "site-a-box"}, ctx)
    for out in (listed, shown):
        blob = json.dumps(out.value, default=str)
        assert box.client_secret not in blob
    assert "foreign://box-app-site-a-box" in listed.value["text"]
    assert "[connector.site-a-box]" in shown.value["text"]


def test_show_hides_credential_material_a_hand_registered_connector_carries(ctx):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    save_connector(ConnectorConfig(name="old-box", kind="box", bronze_root="/b", site="site-a",
                                   params={"folder_id": "1", "session_state_b64": "Y29va2llLWJsb2I="}),
                   state_dir=ctx.state_dir)
    shown = con.show({"name": "old-box"}, ctx)
    listed = con.list_all({}, ctx)

    assert "Y29va2llLWJsb2I=" not in json.dumps(shown.value) + json.dumps(listed.value)
    assert shown.value["params"]["session_state_b64"] == "(set, not shown)"
    assert "browser session (set)" in listed.value["text"]


def test_remove_keeps_the_vault_entry_unless_asked(box, vault, ctx):
    assert _add(box, ctx).ok
    kept = con.remove({"name": "site-a-box"}, ctx)
    assert kept.ok and "box-app-site-a-box" in vault.values
    assert _add(box, ctx).ok
    gone = con.remove({"name": "site-a-box", "drop_vault_entry": True}, ctx)
    assert gone.ok and vault.values == {}
    assert not _registered(ctx)


# ---------------------------------------------------------------- secrets stay in the vault


def test_the_secret_never_reaches_disk_outside_the_vault(box, vault, ctx, tmp_path):
    result = _add(box, ctx)
    assert result.ok
    assert con.test({"name": "site-a-box"}, ctx).ok

    secret = box.client_secret.encode()
    leaked = [p for p in tmp_path.rglob("*") if p.is_file() and secret in p.read_bytes()]
    assert leaked == [], f"the client secret was written to {leaked}"
    assert box.client_secret not in json.dumps(result.value, default=str)
    assert box.client_secret not in " ".join(result.actions_taken)


def test_the_cli_reads_the_secret_from_stdin_never_argv_and_never_prints_it(box, vault, ctx, monkeypatch, capsys):
    from axiom.extensions.builtins.data_platform.commands import cli

    monkeypatch.setattr("sys.stdin", io.StringIO(box.client_secret + "\n"))
    code = cli.main(["connector", "add", "box", "--site", "site-a", "--client-id", box.client_id,
                     "--enterprise-id", box.enterprise_id, "--folder-id", "1001", "--client-secret-stdin"])
    out = capsys.readouterr()

    assert code == 0, out.err
    assert "Sign in to Box: signed in as" in out.out
    assert "[connector.site-a-box]" in out.out
    assert box.client_secret not in out.out + out.err

    with pytest.raises(SystemExit):  # there is no flag that takes the secret itself
        cli._parser().parse_args(["connector", "add", "box", "--client-secret", "x"])


def test_the_pull_resolves_the_reference_setup_wrote(box, vault, ctx):
    """foreign://<entry> resolves for the scheduled pull, not only for setup."""
    assert _add(box, ctx).ok
    from axiom.extensions.builtins.data_platform.sources.box.provider import BoxSourceProvider

    cfg = load_connector("site-a-box", state_dir=ctx.state_dir)
    auth = BoxSourceProvider()._resolve_jwt_auth(cfg)
    assert auth.get_access_token().startswith("tok-")
