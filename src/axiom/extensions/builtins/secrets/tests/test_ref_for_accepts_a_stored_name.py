# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`rotate` and `exposed` take the name you stored it under.

`axi secrets set` takes a bare NAME. `rotate` and `exposed` took a
scheme-qualified SecretRef and nothing else, so the obvious sequence::

    axi secrets set my-key
    axi secrets exposed my-key --where transcript

answered ``bad ref 'my-key': SecretRef missing scheme`` — without naming the
scheme it wanted. `exposed` is the leaked-credential verb: it is reached at
the one moment when the operator is in a hurry and the thing is still live.
Handing them a riddle about URL syntax there is the worst possible time.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.extensions.builtins.secrets.skills.rotate import ref_for


class _Ctx:
    def __init__(self, state_dir):
        self.state_dir = state_dir


@pytest.fixture
def ctx(tmp_path):
    return _Ctx(tmp_path)


class TestABareNameResolves:
    def test_a_stored_name_becomes_its_foreign_ref(self, ctx, monkeypatch):
        monkeypatch.setattr(ForeignCredentialStore, "exists", lambda self, n: n == "my-key")
        assert str(ref_for("my-key", ctx)) == "foreign://my-key"

    def test_an_unstored_name_is_refused(self, ctx, monkeypatch):
        monkeypatch.setattr(ForeignCredentialStore, "exists", lambda self, n: False)
        with pytest.raises(ValueError) as exc:
            ref_for("my-key", ctx)
        assert "no stored credential named 'my-key'" in str(exc.value)

    def test_the_refusal_names_both_forms_it_accepts(self, ctx, monkeypatch):
        """The original error named neither, which is why it was unusable."""
        monkeypatch.setattr(ForeignCredentialStore, "exists", lambda self, n: False)
        with pytest.raises(ValueError) as exc:
            ref_for("my-key", ctx)
        message = str(exc.value)
        assert "axi secrets list" in message
        assert "foreign://" in message and "openbao://" in message


class TestASchemeStillWins:
    def test_a_qualified_ref_is_parsed_not_looked_up(self, ctx, monkeypatch):
        """A ref that names a backend must never be second-guessed against
        the local index — the credential may not be a foreign one at all."""
        def _boom(self, name):  # pragma: no cover — must not be reached
            raise AssertionError("looked up a scheme-qualified ref")

        monkeypatch.setattr(ForeignCredentialStore, "exists", _boom)
        assert str(ref_for("openbao://kv/data/x", ctx)) == "openbao://kv/data/x"

    def test_a_malformed_qualified_ref_still_raises(self, ctx):
        with pytest.raises(ValueError):
            ref_for("://nothing", ctx)


class TestAnUnreadableStoreDoesNotMasquerade:
    def test_a_store_that_raises_is_reported_as_not_found(self, ctx, monkeypatch):
        """No index, no keychain, no permission — none of those mean the
        credential exists, and none should surface as a traceback here."""
        def _raise(self, name):
            raise OSError("keychain unavailable")

        monkeypatch.setattr(ForeignCredentialStore, "exists", _raise)
        with pytest.raises(ValueError, match="no stored credential"):
            ref_for("my-key", ctx)
