# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Edge nodes get Box logins before they exist, each its own, and never the reader's.

Box renews a login on every use and cancels the old one, so a login has one
live copy. A named edge login is made with ``data.box_login``, held in this
host's vault until install, then moved onto the node so only the node renews it.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.data_platform.commands import cli as data_cli
from axiom.extensions.builtins.data_platform.forward.edge_logins import (
    EdgeLoginError,
    attach,
    capture,
    list_logins,
    renew,
    reserve,
)
from axiom.extensions.builtins.data_platform.skills import edge_login
from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.extensions.builtins.secrets.providers.protocol import Capabilities, Secret, SecretRef
from axiom.infra.skills import SkillContext, SkillRegistry

FOLDER_A = "100000000001"
FOLDER_B = "100000000002"


class _Values:
    capabilities = Capabilities(read=True, write=True, delete=True)

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def get(self, ref: SecretRef) -> Secret:
        if ref.path not in self.values:
            raise KeyError(ref.path)
        return Secret(value=self.values[ref.path])

    def put(self, ref: SecretRef, value: bytes) -> None:
        self.values[ref.path] = value

    def delete(self, ref: SecretRef) -> None:
        del self.values[ref.path]

    def list_paths(self, prefix: str) -> list[str]:
        return sorted(p for p in self.values if p.startswith(prefix))


def _store(path):
    values = _Values()
    return ForeignCredentialStore(path, value_store=values), values


def _login(refresh: str) -> str:
    """The one line `data.box_login` writes."""
    return json.dumps(
        {"access_token": "access", "token_type": "bearer", "refresh_token": refresh,
         "expiry": "2026-01-01T00:00:00Z"},
        separators=(",", ":"),
    )


def _held(tmp_path, name, folder, refresh, store, **kw):
    reserve(tmp_path, name, folder, "box-app")
    return capture(tmp_path, name, _login(refresh), store=store, reader_token_stores=[], **kw)


def test_two_future_nodes_each_get_a_login_and_the_ledger_has_neither(tmp_path):
    store, values = _store(tmp_path)
    _held(tmp_path, "edge-01", FOLDER_A, "refresh-one", store)
    _held(tmp_path, "edge-02", FOLDER_B, "refresh-two", store)

    listed = list_logins(tmp_path)
    assert [(r["name"], r["status"], r["app"]) for r in listed] == [
        ("edge-01", "held", "box-app"),
        ("edge-02", "held", "box-app"),
    ]
    ledger = (tmp_path / "edge-box-logins.json").read_text()
    assert "refresh-one" not in ledger and "refresh-two" not in ledger
    assert "refresh-one" not in json.dumps(listed)
    # Stored as written: the line the forwarder hands rclone.
    assert values.values["box-edge-edge-01"] == _login("refresh-one").encode()


def test_the_readers_refresh_token_is_refused_and_nothing_is_stored(tmp_path):
    store, values = _store(tmp_path)
    reader = tmp_path / "oauth_refresh.json"
    reader.write_text(json.dumps({"refresh_token": "refresh-reader"}))
    reserve(tmp_path, "edge-01", FOLDER_A, "box-app")

    with pytest.raises(EdgeLoginError, match="reader already renews"):
        capture(tmp_path, "edge-01", _login("refresh-reader"), store=store, reader_token_stores=[reader])

    assert "box-edge-edge-01" not in values.values
    assert list_logins(tmp_path)[0]["status"] == "reserved"


def test_one_login_cannot_be_held_for_two_nodes(tmp_path):
    store, values = _store(tmp_path)
    _held(tmp_path, "edge-01", FOLDER_A, "refresh-one", store)
    reserve(tmp_path, "edge-02", FOLDER_B, "box-app")
    with pytest.raises(EdgeLoginError, match="already held as edge Box login edge-01"):
        capture(tmp_path, "edge-02", _login("refresh-one"), store=store, reader_token_stores=[])
    assert "box-edge-edge-02" not in values.values


def test_capture_refuses_a_file_that_is_not_a_login(tmp_path):
    store, _ = _store(tmp_path)
    reserve(tmp_path, "edge-01", FOLDER_A, "box-app")
    with pytest.raises(EdgeLoginError, match="not a Box login"):
        capture(tmp_path, "edge-01", "token = abc", store=store, reader_token_stores=[])
    with pytest.raises(EdgeLoginError, match="refresh_token"):
        capture(tmp_path, "edge-01", json.dumps({"access_token": "a"}), store=store, reader_token_stores=[])


def test_attach_moves_the_login_and_copies_the_app(tmp_path):
    src, dest = tmp_path / "hub", tmp_path / "node"
    src.mkdir()
    dest.mkdir()
    store, values = _store(src)
    dest_store, dest_values = _store(dest)
    store.set("box-app", json.dumps({"client_id": "id", "client_secret": "s"}).encode())
    _held(src, "edge-01", FOLDER_A, "refresh-one", store)

    moved = attach(src, "edge-01", "edge-host-01", dest, store=store, dest_store=dest_store)

    assert moved["status"] == "attached" and moved["node"] == "edge-host-01"
    assert "refresh-one" not in json.dumps(moved)
    assert "box-edge-edge-01" not in values.values
    assert dest_values.values["box-edge-edge-01"] == _login("refresh-one").encode()
    assert b"client_secret" in dest_values.values["box-app"]
    assert "box-app" in values.values  # the app is not single-use; the source keeps it

    again = attach(src, "edge-01", "edge-host-01", dest, store=store, dest_store=dest_store)
    assert again["already"] is True
    with pytest.raises(EdgeLoginError, match="already on"):
        attach(src, "edge-01", "other-host", dest, store=store, dest_store=dest_store)


def test_attach_refuses_this_hosts_own_state_dir(tmp_path):
    store, _ = _store(tmp_path)
    _held(tmp_path, "edge-01", FOLDER_A, "refresh-one", store)
    with pytest.raises(EdgeLoginError, match="different directory"):
        attach(tmp_path, "edge-01", "edge-host-01", tmp_path, store=store)


def test_a_held_login_past_its_date_lists_the_steps_and_renews_in_place(tmp_path):
    store, values = _store(tmp_path)
    _held(tmp_path, "edge-01", FOLDER_A, "refresh-old", store)
    ledger = tmp_path / "edge-box-logins.json"
    data = json.loads(ledger.read_text())
    data["logins"]["edge-01"]["refresh_expires_at"] = "2000-01-01"
    ledger.write_text(json.dumps(data))

    listed = list_logins(tmp_path)[0]
    assert listed["needs_renewal"] is True
    assert any("axi data box-login --app box-app" in step for step in listed["renew"])
    assert any("edge-login renew edge-01" in step for step in listed["renew"])

    renewed = renew(tmp_path, "edge-01", _login("refresh-new"), store=store, reader_token_stores=[])
    assert renewed["needs_renewal"] is False
    assert values.values["box-edge-edge-01"] == _login("refresh-new").encode()
    assert "refresh-new" not in ledger.read_text()


def test_a_login_already_on_a_node_is_not_renewed_here(tmp_path):
    src, dest = tmp_path / "hub", tmp_path / "node"
    src.mkdir()
    dest.mkdir()
    store, values = _store(src)
    dest_store, _ = _store(dest)
    dest_store.set("box-app", b'{"client_id":"id","client_secret":"s"}')  # the node already has the app
    _held(src, "edge-01", FOLDER_A, "refresh-one", store)
    attach(src, "edge-01", "edge-host-01", dest, store=store, dest_store=dest_store)

    with pytest.raises(EdgeLoginError, match="edge-host-01") as raised:
        renew(src, "edge-01", _login("refresh-new"), store=store, reader_token_stores=[])
    assert "edge-login renew edge-01" in str(raised.value)
    assert "box-edge-edge-01" not in values.values


def _ctx(tmp_path):
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=_Quiet())


def test_the_skills_reserve_and_list_without_a_login(tmp_path):
    ctx = _ctx(tmp_path)
    reserved = edge_login.reserve({"name": "edge-01", "folder_id": FOLDER_A, "app": "box-app"}, ctx)
    assert reserved.ok, reserved.errors
    assert reserved.value["status"] == "reserved"
    listed = edge_login.list_all({}, ctx)
    assert listed.ok
    assert listed.value["logins"][0]["name"] == "edge-01"
    assert "refresh_token" not in json.dumps(listed.value)
    missing = edge_login.capture({"name": "edge-01", "login_file": str(tmp_path / "nope.json")}, ctx)
    assert not missing.ok and "no Box login file" in missing.errors[0]


def test_each_cli_verb_resolves_to_its_own_skill():
    parser = data_cli._parser()
    for argv, skill in [
        (["edge-login", "reserve", "edge-01", "--folder-id", FOLDER_A, "--app", "box-app"], "edge_login_reserve"),
        (["edge-login", "capture", "edge-01", "--login-file", "f.json"], "edge_login_capture"),
        (["edge-login", "renew", "edge-01", "--login-file", "f.json"], "edge_login_renew"),
        (["edge-login", "attach", "edge-01", "--node", "h", "--dest-state-dir", "/d"], "edge_login_attach"),
        (["edge-login", "list"], "edge_login_list"),
    ]:
        assert data_cli.resolve_skill(parser.parse_args(argv)) == (skill, None)


class _Quiet:
    def info(self, *args, **kwargs):
        return None

    warning = error = debug = info
