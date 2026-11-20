"""The gate's OIDC client secret can be read from the vault, by name.

It could only come from an environment variable or a plaintext file, which is
the exposure the vault exists to prevent: a secret in a file is readable by
anything that can read the file, and one in the environment is readable from
the process table by the same user.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.extensions.builtins.webgate.oidc import OidcSignIn


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    store = ForeignCredentialStore(tmp_path)
    store.set("sign-in-client", b"from-the-vault")
    return store


def _env(**extra):
    return {
        "AXIOM_GATE_OIDC_PROVIDER": "entra",
        "AXIOM_GATE_OIDC_TENANT": "00000000-0000-0000-0000-000000000000",
        "AXIOM_GATE_OIDC_CLIENT_ID": "client",
        **extra,
    }


def test_a_secret_named_in_the_vault_is_read_from_it(vault):
    cfg = OidcSignIn.from_env(_env(AXIOM_GATE_OIDC_CLIENT_SECRET_VAULT="sign-in-client"))
    assert cfg.client_secret == "from-the-vault"


def test_a_missing_vault_entry_is_refused_rather_than_ignored(vault):
    with pytest.raises(ValueError, match="no-such-entry"):
        OidcSignIn.from_env(_env(AXIOM_GATE_OIDC_CLIENT_SECRET_VAULT="no-such-entry"))


def test_with_no_secret_named_the_client_is_public(vault):
    assert OidcSignIn.from_env(_env()).client_secret is None
