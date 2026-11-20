# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""A connection uses the secrets store, not a copied token that rotation leaves behind."""

from __future__ import annotations

from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.infra.connections import Connection, ConnectionRegistry, get_credential


def test_an_overlay_name_beats_the_stale_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    monkeypatch.setattr("axiom.infra.connections.get_user_state_dir", lambda: tmp_path)
    ForeignCredentialStore(tmp_path).set("partner-vcs-token", b"fresh-token")
    token = tmp_path / "credentials" / "codehost" / "token"
    token.parent.mkdir(parents=True)
    token.write_text("stale-token")
    token.chmod(0o600)
    (tmp_path / "connections-secrets.json").write_text('{"codehost": "partner-vcs-token"}')

    registry = ConnectionRegistry()
    registry.register(Connection(name="codehost", display_name="Code host", kind="api"))
    assert get_credential(
        "codehost", registry=registry, credentials_dir=tmp_path / "credentials"
    ) == "fresh-token"


def test_a_missing_named_secret_does_not_fall_through_to_the_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    monkeypatch.setattr("axiom.infra.connections.get_user_state_dir", lambda: tmp_path)
    token = tmp_path / "credentials" / "codehost" / "token"
    token.parent.mkdir(parents=True)
    token.write_text("stale-token")
    token.chmod(0o600)
    registry = ConnectionRegistry()
    registry.register(Connection(
        name="codehost", display_name="Code host", kind="api", credential_secret="partner-vcs-token",
    ))
    assert get_credential(
        "codehost", registry=registry, credentials_dir=tmp_path / "credentials"
    ) is None
