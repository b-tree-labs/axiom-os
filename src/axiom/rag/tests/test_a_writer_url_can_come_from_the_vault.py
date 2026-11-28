# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The RAG writer's database URL can come from the vault, by name.

``rag.write_database_url`` (#2100) holds the whole ``postgresql://`` URL of the role that owns the
RAG tables, password included, in plain node settings. The node already keeps credentials in the
vault (ADR-218), so ``rag.write_database_vault`` names a vault entry whose secret is that URL, read
when a writer connects.

- VAULT-A1: writers resolve ``DATABASE_URL``, then ``rag.write_database_vault``, then
  ``rag.write_database_url``, then the read path; the source label names the one used.
- VAULT-A2: a vault setting whose entry is missing, empty, unreadable or not a Postgres URL fails
  closed for writers, naming the setting and the entry and saying how to fix it. It never falls
  through to the plain setting or the read path.
- VAULT-A3: readers are unaffected by the vault setting, even a broken one.
- VAULT-A4: the URL never appears in an error, the source label or a repr.
- VAULT-A5: the secret is read at connect time, not cached and not written to settings.
- VAULT-A6: every writer honours it: ``axi rag`` writers and the data-platform writer DSN.
- VAULT-A7: the messages that tell an operator what to set name the vault setting first.
"""

from __future__ import annotations

import argparse

import pytest

from axiom.rag import store_factory

SECRET_PASSWORD = "s3cret-pa55word-xyz"
VAULT_URL = f"postgresql://owner:{SECRET_PASSWORD}@db.example.org/rag"
PLAIN_OWNER = "postgresql://plainowner@db.example.org/rag"
READER = "postgresql://reader@db.example.org/rag"
OVERRIDE = "postgresql://override@db.example.org/rag"
ENTRY = "rag-writer-url"

_DB_ENV = ("DATABASE_URL", "DP1_RAG_DSN", "AXIOM_DB_URL")


class _Settings:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture
def settings(monkeypatch):
    import os

    from axiom.rag import cli

    values: dict = {"rag.database_url": READER}
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore", lambda: _Settings(values)
    )
    for name in _DB_ENV:
        monkeypatch.delenv(name, raising=False)
    # cli._load_env writes os.environ and a module global directly; give it a copy to write to.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(cli, "_operator_database_url", cli._operator_database_url)
    monkeypatch.setattr("axiom.infra.serving_mode.resolve_mode", lambda: None)
    return values


@pytest.fixture
def vault(settings, tmp_path, monkeypatch):
    """A file-backed vault in a temporary state dir, holding the owner's URL under ``ENTRY``."""
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    store = ForeignCredentialStore(tmp_path)
    store.set(ENTRY, VAULT_URL.encode())
    return store


def _no_secret_in(text: str) -> None:
    assert SECRET_PASSWORD not in text, text
    assert VAULT_URL not in text, text


# ---------------------------------------------------------------------------
# VAULT-A1: precedence
# ---------------------------------------------------------------------------


def test_vault_a1_the_vault_entry_is_the_writers_url(settings, vault):
    """VAULT-A1: the named vault entry is the store a writer opens; readers keep the read path."""
    settings["rag.write_database_vault"] = ENTRY

    resolved = store_factory.resolve_write_url()

    assert resolved.url == VAULT_URL
    assert resolved.origin == f"rag.write_database_vault setting (vault entry {ENTRY!r})"
    assert store_factory.resolve_store_url().url == READER


def test_vault_a1_the_vault_setting_beats_the_plain_setting(settings, vault):
    """VAULT-A1: with both set, the vault entry wins over ``rag.write_database_url``."""
    settings["rag.write_database_vault"] = ENTRY
    settings["rag.write_database_url"] = PLAIN_OWNER

    assert store_factory.resolve_write_url().url == VAULT_URL


def test_vault_a1_database_url_still_overrides_the_vault(settings, vault, monkeypatch):
    """VAULT-A1: ``DATABASE_URL`` stays the highest-precedence override."""
    settings["rag.write_database_vault"] = ENTRY
    monkeypatch.setenv("DATABASE_URL", OVERRIDE)

    assert store_factory.resolve_write_url() == (OVERRIDE, "DATABASE_URL env", "")


def test_vault_a1_with_no_vault_setting_the_plain_setting_is_used(settings, vault):
    """VAULT-A1: an empty vault setting changes nothing for a node that uses the plain one."""
    settings["rag.write_database_vault"] = ""
    settings["rag.write_database_url"] = PLAIN_OWNER

    assert store_factory.resolve_write_url() == (PLAIN_OWNER, "rag.write_database_url setting", "")


def test_vault_a1_the_vault_setting_has_a_declared_default():
    """VAULT-A1: the setting is declared, empty by default, so ``settings get`` knows it."""
    from axiom.extensions.builtins.settings.store import _DEFAULTS

    assert _DEFAULTS["rag.write_database_vault"] == ""


# ---------------------------------------------------------------------------
# VAULT-A2: fail closed
# ---------------------------------------------------------------------------


def test_vault_a2_a_missing_entry_fails_closed_naming_the_setting_and_entry(settings, vault):
    """VAULT-A2: no fall-through to ``rag.write_database_url`` or the read path."""
    settings["rag.write_database_vault"] = "no-such-entry"
    settings["rag.write_database_url"] = PLAIN_OWNER

    with pytest.raises(store_factory.WriteUrlUnavailable) as caught:
        store_factory.resolve_write_url()

    message = str(caught.value)
    assert "rag.write_database_vault" in message, message
    assert "no-such-entry" in message, message
    assert "axi vault set" in message, message
    assert "\n" not in message.strip(), message


def test_vault_a2_an_empty_entry_fails_closed(settings, vault):
    """VAULT-A2: an entry that holds nothing is refused, not read as unconfigured."""
    vault.set("blank", b"   ")
    settings["rag.write_database_vault"] = "blank"

    with pytest.raises(store_factory.WriteUrlUnavailable, match="blank"):
        store_factory.resolve_write_url()


def test_vault_a2_an_unreadable_vault_fails_closed_without_the_backend_text(settings, vault, monkeypatch):
    """VAULT-A2: a vault that cannot be read fails closed; the backend's own text is not echoed."""
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    def _boom(self, name):
        raise RuntimeError(f"backend exploded while holding {VAULT_URL}")

    monkeypatch.setattr(ForeignCredentialStore, "get", _boom)
    settings["rag.write_database_vault"] = ENTRY
    settings["rag.write_database_url"] = PLAIN_OWNER

    with pytest.raises(store_factory.WriteUrlUnavailable) as caught:
        store_factory.resolve_write_url()

    message = str(caught.value)
    assert ENTRY in message and "rag.write_database_vault" in message, message
    _no_secret_in(message)
    _no_secret_in(repr(caught.value))


def test_vault_a2_an_entry_that_is_not_a_postgres_url_fails_closed_without_echoing_it(settings, vault):
    """VAULT-A2/A4: a bare password stored by mistake is refused, and never quoted back."""
    vault.set("just-a-password", SECRET_PASSWORD.encode())
    settings["rag.write_database_vault"] = "just-a-password"

    with pytest.raises(store_factory.WriteUrlUnavailable) as caught:
        store_factory.resolve_write_url()

    message = str(caught.value)
    assert "just-a-password" in message and "postgresql://" in message, message
    _no_secret_in(message)


def test_vault_a2_the_data_platform_writer_fails_closed_too(settings, vault, monkeypatch):
    """VAULT-A2: ``resolve_write_dsn`` does not fall through to the platform's own URL."""
    from axiom.extensions.builtins.data_platform._dsn import resolve_write_dsn

    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://platform@db.example.org/axiom")
    settings["rag.write_database_vault"] = "no-such-entry"

    with pytest.raises(store_factory.WriteUrlUnavailable, match="no-such-entry"):
        resolve_write_dsn({})


# ---------------------------------------------------------------------------
# The rag CLI
# ---------------------------------------------------------------------------


def _rag_cli(monkeypatch):
    """The rag CLI with its store factory recorded; nothing connects."""
    from axiom.rag import cli

    opened: list[tuple[str, bool]] = []

    class _Store:
        def connect(self):
            pass

        def close(self):
            pass

        def generation_report(self):
            return []

    def _create(url, *, ensure_schema=True):
        opened.append((url, ensure_schema))
        return _Store()

    monkeypatch.setattr(store_factory, "create_store", _create)
    monkeypatch.setattr(cli, "_first_reachable_url", lambda: "")
    cli._load_env()
    return cli, opened


def test_vault_a2_the_rag_cli_writer_exits_with_the_sentence(settings, vault, monkeypatch, capsys):
    """VAULT-A2: ``axi rag`` writers exit 1 with the sentence, never opening another store."""
    settings["rag.write_database_vault"] = "no-such-entry"
    settings["rag.write_database_url"] = PLAIN_OWNER
    cli, opened = _rag_cli(monkeypatch)

    with pytest.raises(SystemExit) as exited:
        cli._get_store()

    assert exited.value.code == 1
    assert opened == []
    err = capsys.readouterr().err
    assert "rag.write_database_vault" in err and "no-such-entry" in err and "Traceback" not in err, err


# ---------------------------------------------------------------------------
# VAULT-A3: readers unaffected
# ---------------------------------------------------------------------------


def test_vault_a3_readers_ignore_even_a_broken_vault_setting(settings, vault, monkeypatch):
    """VAULT-A3: the read path and a read-only CLI command never consult the vault setting."""
    settings["rag.write_database_vault"] = "no-such-entry"

    assert store_factory.resolve_store_url().url == READER
    cli, opened = _rag_cli(monkeypatch)
    cli.cmd_generations(argparse.Namespace(json=True))
    assert opened == [(READER, False)], opened


# ---------------------------------------------------------------------------
# VAULT-A4: no secret in label or repr
# ---------------------------------------------------------------------------


def test_vault_a4_the_label_and_repr_never_carry_the_url(settings, vault):
    """VAULT-A4: what a log or a doctor row could print holds the entry name, not the secret."""
    settings["rag.write_database_vault"] = ENTRY

    resolved = store_factory.resolve_write_url()

    _no_secret_in(resolved.origin)
    _no_secret_in(repr(resolved))
    _no_secret_in(str(resolved))
    assert ENTRY in repr(resolved)


# ---------------------------------------------------------------------------
# VAULT-A5: read at connect time
# ---------------------------------------------------------------------------


def test_vault_a5_a_rotated_entry_is_used_on_the_next_connect_and_settings_are_untouched(settings, vault):
    """VAULT-A5: nothing is cached, and the URL is never copied into the settings."""
    settings["rag.write_database_vault"] = ENTRY
    before = dict(settings)

    assert store_factory.resolve_write_url().url == VAULT_URL
    rotated = "postgresql://owner:rotated@db.example.org/rag"
    vault.set(ENTRY, rotated.encode())
    assert store_factory.resolve_write_url().url == rotated

    assert settings == before


# ---------------------------------------------------------------------------
# VAULT-A6: every writer honours it
# ---------------------------------------------------------------------------


def test_vault_a6_the_rag_cli_writer_opens_the_vault_url(settings, vault, monkeypatch):
    """VAULT-A6: a writing ``axi rag`` command opens the vault entry and ensures the schema."""
    settings["rag.write_database_vault"] = ENTRY
    settings["rag.write_database_url"] = PLAIN_OWNER
    cli, opened = _rag_cli(monkeypatch)

    cli._get_store()

    assert opened == [(VAULT_URL, True)]


def test_vault_a6_the_data_platform_writer_dsn_is_the_vault_url(settings, vault, monkeypatch):
    """VAULT-A6: reindex, relabel, ingest and backfill resolve the vault entry."""
    from axiom.extensions.builtins.data_platform._dsn import resolve_write_dsn

    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://platform@db.example.org/axiom")
    settings["rag.write_database_vault"] = ENTRY
    settings["rag.write_database_url"] = PLAIN_OWNER

    assert resolve_write_dsn({}) == VAULT_URL
    monkeypatch.setenv("DATABASE_URL", OVERRIDE)
    assert resolve_write_dsn({}) == OVERRIDE, "DATABASE_URL is the operator's override"


def test_vault_a6_one_helper_reads_the_writer_settings():
    """VAULT-A6: no caller reads ``rag.write_database_url`` itself; they go through one helper."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in ("rag/cli.py", "extensions/builtins/data_platform/_dsn.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "configured_write_url" in text, f"{rel} does not use the shared helper"
        assert "write_url_setting" not in text, f"{rel} reads the plain setting directly"


# ---------------------------------------------------------------------------
# VAULT-A7: the operator's fix names the vault setting first
# ---------------------------------------------------------------------------


def _vault_named_first(text: str) -> None:
    assert "rag.write_database_vault" in text, text
    assert "rag.write_database_url" in text, text
    assert text.index("rag.write_database_vault") < text.index("rag.write_database_url"), text


def test_vault_a7_refused_schema_ddl_names_the_vault_setting_first():
    """VAULT-A7: the one-sentence refusal from #2100 offers the vault setting first."""
    from axiom.rag.store import _schema_refused

    message = str(_schema_refused(PermissionError("permission denied for schema public")))

    _vault_named_first(message)
    assert "\n" not in message.strip(), message


def test_vault_a7_the_doctor_fix_names_the_vault_setting_first():
    """VAULT-A7: the doctor row for a schema behind the code offers the vault setting first."""
    from axiom.rag.ingest import schema_doctor_rows

    class _Behind:
        def schema_behind(self):
            return {"documents": ["parser"]}

    [row] = schema_doctor_rows(_Behind())

    _vault_named_first(row["fix"])
