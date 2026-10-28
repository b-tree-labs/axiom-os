# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The sweep can finally tell custody from a loose copy.

`secrets.discover` answers "is this credential already in custody?" by
hashing what it finds and asking whether that digest is in an index of what
the store holds. Nothing ever wrote that index — 22 credentials on a real
machine, 0 fingerprints — and `vault.sweep` passed none, so every finding
landed in `unchecked`, `unmanaged` was always zero, and an hourly sweep
reported "clean" over a host it had not checked. A check that could not
fail.

Two halves, both needed and neither sufficient alone: the store derives a
digest when a value is stored, and the sweep hands those digests to the
scanner.

The value never leaves custody. The digest is one-way and truncated, which
is what makes a discovery report safe to paste into a ticket — and is also
why it is DERIVED in `set()` rather than accepted from a caller: a
supplied fingerprint would let the index claim custody of a value it never
held.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets.discovery.model import fingerprint
from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.extensions.builtins.secrets.skills import discover
from axiom.extensions.builtins.vault.skills import sweep

ANTHROPIC = "sk-ant-api03-" + "A" * 80
OTHER = "sk-ant-api03-" + "B" * 80


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    return ForeignCredentialStore(tmp_path)


class _Ctx:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.registry = None


class TestTheStoreRecordsWhatItHolds:
    def test_a_stored_credential_gets_a_digest(self, store):
        store.set("k", ANTHROPIC.encode(), provider="guided")
        assert store.list()[0]["fingerprint"] == fingerprint(ANTHROPIC)

    def test_the_digest_is_not_the_value(self, store):
        store.set("k", ANTHROPIC.encode(), provider="guided")
        fp = store.list()[0]["fingerprint"]
        assert ANTHROPIC not in fp and len(fp) < 40

    def test_a_caller_may_not_supply_one(self, store):
        """It asserts custody. Accepting it from a caller would let the index
        claim a value the store never held."""
        with pytest.raises(ValueError, match="derived from the value"):
            store.set("k", ANTHROPIC.encode(), fingerprint="deadbeef")

    def test_rotating_the_value_moves_the_digest(self, store):
        store.set("k", ANTHROPIC.encode(), provider="guided")
        first = store.list()[0]["fingerprint"]
        store.set("k", OTHER.encode(), provider="guided")
        assert store.list()[0]["fingerprint"] != first


class TestTheSweepSuppliesTheIndex:
    def test_a_copy_of_a_held_credential_is_a_consumer_not_a_leak(
        self, store, tmp_path, monkeypatch
    ):
        """The whole point: the same value, found loose, is somewhere to
        update when it rotates — not an unmanaged credential."""
        monkeypatch.setattr(discover, "_LAST_STALE", [])
        store.set("k", ANTHROPIC.encode(), provider="guided")
        (tmp_path / "loose.env").write_text(
            f"export COPY={ANTHROPIC}\n", encoding="utf-8"
        )
        result = sweep.run({"env_root": str(tmp_path)}, _Ctx(tmp_path))
        assert result.value["sweep"]["consumers"] == 1
        assert result.value["sweep"]["unmanaged"] == 0

    def test_a_credential_the_store_never_held_is_unmanaged(
        self, store, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(discover, "_LAST_STALE", [])
        store.set("k", ANTHROPIC.encode(), provider="guided")
        (tmp_path / "stray.env").write_text(f"export STRAY={OTHER}\n", encoding="utf-8")
        result = sweep.run({"env_root": str(tmp_path)}, _Ctx(tmp_path))
        assert result.value["sweep"]["unmanaged"] == 1
        assert result.ok is False

    def test_an_empty_store_still_refuses_to_report_clean(
        self, store, tmp_path, monkeypatch
    ):
        """Nothing in custody is not the same as nothing loose. With no index
        the sweep cannot classify, and must say so rather than pass."""
        monkeypatch.setattr(discover, "_LAST_STALE", [])
        (tmp_path / "stray.env").write_text(f"export STRAY={OTHER}\n", encoding="utf-8")
        result = sweep.run({"env_root": str(tmp_path)}, _Ctx(tmp_path))
        assert result.value["sweep"]["unverifiable"] is True
        assert result.ok is False

    def test_an_unreadable_store_is_not_a_clean_bill(self, tmp_path, monkeypatch):
        monkeypatch.setattr(discover, "_LAST_STALE", [])
        monkeypatch.setattr(
            ForeignCredentialStore, "list",
            lambda self: (_ for _ in ()).throw(OSError("no index")),
        )
        (tmp_path / "stray.env").write_text(f"export STRAY={OTHER}\n", encoding="utf-8")
        result = sweep.run({"env_root": str(tmp_path)}, _Ctx(tmp_path))
        assert result.value["sweep"]["unverifiable"] is True

    def test_reading_the_index_never_opens_a_credential(self, store, tmp_path, monkeypatch):
        """`list()` is metadata-only, so a sweep learns what it holds without
        touching a single value."""
        store.set("k", ANTHROPIC.encode(), provider="guided")
        monkeypatch.setattr(
            ForeignCredentialStore, "get",
            lambda self, n: (_ for _ in ()).throw(AssertionError("opened a value")),
        )
        assert sweep._known_fingerprints(_Ctx(tmp_path)) == [fingerprint(ANTHROPIC)]


class TestBackfillingCredentialsStoredBeforeTheIndex:
    """`set()` only indexes on write, so an existing machine gets nothing.

    Twenty-two credentials on a real host carried no fingerprint, which
    would have made this a new-installs-only feature — the exact shape of
    failure it was built to fix. `secrets.reindex` is the one-time
    backfill, and it is a separate operator-invoked verb because it is the
    only path here that opens stored values.
    """

    def _reindex(self, store, **params):
        from axiom.extensions.builtins.secrets.skills import reindex

        return reindex.run({"_store": store, **params}, None)

    def test_it_fills_in_a_missing_digest(self, store):
        store.set("k", ANTHROPIC.encode(), provider="guided")
        store.update_metadata("k", fingerprint=None)
        assert not store.list()[0].get("fingerprint")
        r = self._reindex(store)
        assert r.ok and r.value["indexed"] == 1
        assert store.list()[0]["fingerprint"] == fingerprint(ANTHROPIC)

    def test_a_dry_run_opens_nothing(self, store, monkeypatch):
        """The point of --dry-run is to answer "how many?" without
        decrypting anything."""
        store.set("k", ANTHROPIC.encode(), provider="guided")
        store.update_metadata("k", fingerprint=None)
        monkeypatch.setattr(
            ForeignCredentialStore, "get",
            lambda self, n: (_ for _ in ()).throw(AssertionError("opened a value")),
        )
        r = self._reindex(store, dry_run=True)
        assert r.ok and r.value["indexed"] == 0 and r.value["names"] == ["k"]

    def test_an_already_indexed_store_is_a_no_op(self, store):
        store.set("k", ANTHROPIC.encode(), provider="guided")
        r = self._reindex(store)
        assert r.ok and r.value["indexed"] == 0 and r.value["already"] == 1

    def test_one_unreadable_credential_does_not_abandon_the_rest(
        self, store, monkeypatch
    ):
        store.set("good", ANTHROPIC.encode(), provider="guided")
        store.set("bad", OTHER.encode(), provider="guided")
        for n in ("good", "bad"):
            store.update_metadata(n, fingerprint=None)
        real = ForeignCredentialStore.get

        def flaky(self, name):
            if name == "bad":
                raise OSError("keychain said no")
            return real(self, name)

        monkeypatch.setattr(ForeignCredentialStore, "get", flaky)
        r = self._reindex(store)
        assert r.value["indexed"] == 1 and r.value["failed"] == 1
        assert r.ok is False and "bad" in r.errors[0]
