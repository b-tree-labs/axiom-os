# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Open WebUI prompt projector.

These encode the regression that broke the "/" command palette in production:
Open WebUI serves the palette from its ``prompt`` table, validated by a Pydantic
model that REQUIRES ``tags`` to be a list and ``data``/``meta`` to be dicts, and
only shows rows with a truthy ``is_active``. A prompt written with NULL JSON
columns or a falsy ``is_active`` makes the whole ``/api/v1/prompts/`` response
fail validation (empty palette). The projector must always write valid empty
JSON + ``is_active=1``, heal legacy NULLs on re-sync, and grant public read.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from axiom.extensions.builtins.commands.openwebui_projector import (
    PromptEntry,
    parse_command_catalog,
    project_prompts,
)

ENTRIES = [
    PromptEntry(command="/day", name="Daily picture", content="Summarize {{date}}"),
    PromptEntry(command="/status", name="Status now", content="Current status"),
]


def _mk_webui_db(path) -> None:
    """A faithful minimal replica of Open WebUI's prompt + access_grant schema."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE user (
            id TEXT PRIMARY KEY, email TEXT, role TEXT, created_at INTEGER
        );
        CREATE TABLE prompt (
            id TEXT PRIMARY KEY, command TEXT UNIQUE, user_id TEXT, name TEXT,
            content TEXT, data JSON, meta JSON, tags JSON, is_active BOOLEAN,
            version_id TEXT, created_at BIGINT, updated_at BIGINT
        );
        CREATE TABLE access_grant (
            id TEXT PRIMARY KEY, resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
            principal_type TEXT NOT NULL, principal_id TEXT NOT NULL,
            permission TEXT NOT NULL, created_at BIGINT NOT NULL,
            UNIQUE(resource_type, resource_id, principal_type, principal_id, permission)
        );
        INSERT INTO user VALUES ('admin-1', 'admin@localhost', 'admin', 1);
        """
    )
    db.commit()
    db.close()


def test_insert_sets_valid_json_and_active(tmp_path):
    db_path = tmp_path / "webui.db"
    _mk_webui_db(db_path)

    res = project_prompts(db_path, ENTRIES)

    assert (res.inserted, res.updated, res.total) == (2, 0, 2)
    db = sqlite3.connect(db_path)
    rows = db.execute(
        "select command, is_active, tags, data, meta, user_id from prompt order by command"
    ).fetchall()
    assert [r[0] for r in rows] == ["/day", "/status"]
    for command, is_active, tags, data, meta, user_id in rows:
        assert is_active == 1, f"{command} is not active -> hidden from palette"
        assert json.loads(tags) == [], f"{command} tags must be a list, never NULL"
        assert json.loads(data) == {}
        assert json.loads(meta) == {}
        assert user_id == "admin-1", "prompts must be owned by the admin user"
    grants = db.execute(
        "select count(*) from access_grant where resource_type='prompt' "
        "and principal_type='user' and principal_id='*' and permission='read'"
    ).fetchone()[0]
    assert grants == 2, "each prompt needs a public-read grant to appear for non-owners"


def test_resync_is_idempotent_and_heals_legacy_nulls(tmp_path):
    db_path = tmp_path / "webui.db"
    _mk_webui_db(db_path)
    project_prompts(db_path, ENTRIES)

    # Simulate a row written by the old buggy adapter: NULL JSON + falsy is_active.
    db = sqlite3.connect(db_path)
    db.execute(
        "update prompt set tags=NULL, data=NULL, meta=NULL, is_active='' where command='/day'"
    )
    db.commit()
    db.close()

    res = project_prompts(db_path, ENTRIES)  # re-sync must self-heal

    assert (res.inserted, res.updated, res.total) == (0, 2, 2)
    db = sqlite3.connect(db_path)
    is_active, tags, data, meta = db.execute(
        "select is_active, tags, data, meta from prompt where command='/day'"
    ).fetchone()
    assert is_active == 1
    assert json.loads(tags) == []
    assert json.loads(data) == {}
    assert json.loads(meta) == {}


def test_public_grants_not_duplicated_on_resync(tmp_path):
    db_path = tmp_path / "webui.db"
    _mk_webui_db(db_path)
    project_prompts(db_path, ENTRIES)
    project_prompts(db_path, ENTRIES)

    db = sqlite3.connect(db_path)
    assert db.execute("select count(*) from access_grant").fetchone()[0] == 2


def test_content_and_name_updated_on_resync(tmp_path):
    db_path = tmp_path / "webui.db"
    _mk_webui_db(db_path)
    project_prompts(db_path, ENTRIES)

    project_prompts(
        db_path,
        [PromptEntry(command="/day", name="New title", content="New body")],
    )

    db = sqlite3.connect(db_path)
    name, content = db.execute(
        "select name, content from prompt where command='/day'"
    ).fetchone()
    assert name == "New title"
    assert content == "New body"


def test_missing_prompt_table_raises(tmp_path):
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()
    with pytest.raises(ValueError, match="prompt"):
        project_prompts(db_path, ENTRIES)


def test_parse_command_catalog_and_project_end_to_end(tmp_path):
    catalog = (
        "# Site command catalog\n\n"
        "### /day — Daily picture\n"
        "Summarize {{date}} operations.\n"
        "Second line of the body.\n\n"
        "### /status — Status now\n"
        "Current status readout.\n"
    )
    entries = parse_command_catalog(catalog)
    assert [e.command for e in entries] == ["/day", "/status"]
    assert entries[0].name == "Daily picture"
    assert "Summarize {{date}} operations." in entries[0].content
    assert "Second line of the body." in entries[0].content
    assert entries[1].content == "Current status readout."

    # the parsed entries project cleanly into a fresh Open WebUI db
    db_path = tmp_path / "webui.db"
    _mk_webui_db(db_path)
    res = project_prompts(db_path, entries)
    assert (res.inserted, res.total) == (2, 2)
