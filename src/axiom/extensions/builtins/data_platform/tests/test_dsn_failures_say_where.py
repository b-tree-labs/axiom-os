# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A database failure on a READ verb says where the DSN came from.

Found by calling the gold MCP tools as a colleague would and getting:

    ProgrammingError: invalid dsn: missing "=" after
    "postgresql+psycopg2://axiom:axiom@localhost:5432/axiom_db"

That names the string and not which of four places produced it, and the fix is
different in each: an explicit param is the caller's to correct, an env var
belongs to whoever exported it, and the platform default means nothing was
configured at all. `dsn_source()` has existed to answer exactly this since it
was written, and only `backup` was asking it — so every read verb, which is
what a newcomer reaches for first, said the least.

ADR-157 Phase 0.2 then removed the first of the four places: a request can no
longer carry a `dsn=` at all (a caller could redirect the database), so the
source a failure names is always the deployment's — an env var or the platform
default — and a passed param is simply not consulted.
"""

from __future__ import annotations

from axiom.infra.skills import SkillContext, SkillRegistry


def _ctx(tmp_path):
    import logging

    return SkillContext(
        registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t")
    )


def _failure(tmp_path, params):
    from axiom.extensions.builtins.data_platform.skills import gold

    return gold.tables(params, _ctx(tmp_path))


def test_an_explicit_dsn_param_is_not_consulted(tmp_path, monkeypatch):
    # ADR-157 Phase 0.2: the deployment chooses the database. The param that
    # used to redirect the connection is ignored; the failure that follows
    # names the deployment's own source.
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.delenv("DP1_RAG_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = _failure(tmp_path, {"dsn": "postgresql://evil@attacker:5432/steal"})
    assert not result.ok
    assert not any("attacker" in e for e in result.errors), result.errors
    assert any("AXIOM_DB_URL" in e for e in result.errors), result.errors


def test_an_env_dsn_is_named_as_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://nobody@127.0.0.1:1/none")
    result = _failure(tmp_path, {})
    assert not result.ok
    assert any("AXIOM_DB_URL" in e for e in result.errors), result.errors


def test_the_original_error_is_still_there(tmp_path, monkeypatch):
    """The source is added beside the failure, never instead of it — the
    database's own words are what says WHAT went wrong."""
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.delenv("DP1_RAG_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = _failure(tmp_path, {})
    assert len(result.errors) >= 2
    assert any("database:" in e for e in result.errors)
    assert not all("database:" in e for e in result.errors)
