"""The gate a node mounts signs in the accounts `axi gate adduser` wrote.

`adduser` writes to `$AXIOM_GATE_USERS_FILE`, and nothing on a plain node read
that file: the mounted gate used the process-wide store, which starts empty and
nothing in production fills. So every sign-in on a fresh node failed with
"Invalid email or password" for an account the CLI had just reported adding.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.webgate import mount
from axiom.extensions.builtins.webgate.skills.adduser import run as adduser


def _client():
    app = FastAPI()
    spec = mount.mount_spec()
    app.include_router(spec.router)  # the composed app adds no prefix
    return TestClient(app)


def _login(client, password):
    return client.post(
        "/gate/login",
        data={"email": "person@example.org", "password": password, "next": "/"},
        follow_redirects=False,
    )


def test_an_account_added_with_the_cli_can_sign_in(tmp_path, monkeypatch):
    accounts = tmp_path / "users.json"
    monkeypatch.setenv("AXIOM_GATE_USERS_FILE", str(accounts))
    added = adduser({"email": "person@example.org", "roles": ["operator"]}, None)
    assert added.ok, added.errors

    response = _login(_client(), added.value["password"])
    assert response.status_code == 303


def test_an_account_added_after_start_is_seen_without_a_restart(tmp_path, monkeypatch):
    accounts = tmp_path / "users.json"
    monkeypatch.setenv("AXIOM_GATE_USERS_FILE", str(accounts))
    client = _client()
    added = adduser({"email": "person@example.org", "roles": ["operator"]}, None)

    assert _login(client, added.value["password"]).status_code == 303


def test_a_wrong_password_is_still_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_GATE_USERS_FILE", str(tmp_path / "users.json"))
    adduser({"email": "person@example.org", "roles": ["operator"]}, None)

    response = _login(_client(), "not-the-password-1A!")
    assert response.status_code == 401 and "Invalid email or password" in response.text


def test_with_no_accounts_file_nobody_signs_in(monkeypatch):
    monkeypatch.delenv("AXIOM_GATE_USERS_FILE", raising=False)

    response = _login(_client(), "anything-at-all-1A!")
    assert response.status_code == 401 and "Invalid email or password" in response.text
