# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Putting one site's data back in one place.

`site_identity` stops a rename forking a site again. This is the other
half — the rows already written — and it is the half that matters for the
two sites this was found on: 246 GB under one id and 7,307 rows under
another for one reactor, and roughly 28.8M rows under a flow loop's
former name.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.skills import site_rename


class _Result:
    def __init__(self, value=0, rowcount=0):
        self._value = value
        self.rowcount = rowcount

    def scalar_one(self):
        return self._value

    def fetchall(self):
        return self._value


class _Connection:
    """A connection that answers the three statements this skill makes."""

    def __init__(self, columns, counts, unique_keys=(), collisions=0):
        self.columns = columns
        self.counts = counts
        self.unique_keys = list(unique_keys)
        self.collisions = collisions
        self.updates: list[tuple[str, dict]] = []
        self.rolled_back = False

    def execute(self, statement, params=None):
        sql = str(statement)
        if "information_schema.columns" in sql:
            return _Result(self.columns)
        # Checked BEFORE the plain count: the collision probe is also a
        # `SELECT count(*)`, and answering it with the row count made a
        # test assert on a number that came from the wrong query.
        if "pg_index" in sql:
            return _Result(self.unique_keys)
        if " JOIN " in sql and "count(*)" in sql.lower():
            return _Result(self.collisions)
        if sql.strip().upper().startswith("SELECT COUNT"):
            for (schema, table, _column), n in self.counts.items():
                if f'"{schema}"."{table}"' in sql:
                    return _Result(n if params["old"] == "old-id" else 0)
            return _Result(0)
        if sql.strip().upper().startswith("UPDATE"):
            self.updates.append((sql, dict(params or {})))
            for (schema, table, _column), n in self.counts.items():
                if f'"{schema}"."{table}"' in sql:
                    return _Result(rowcount=n)
            return _Result(rowcount=0)
        raise AssertionError(f"unexpected statement: {sql}")

    def rollback(self):
        self.rolled_back = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Engine:
    def __init__(self, connection):
        self._connection = connection

    def begin(self):
        return self._connection


@pytest.fixture
def platform(monkeypatch):
    columns = [
        ("silver", "signals", "site"),
        ("gold", "signal_daily", "site"),
        ("webapp", "site_catalog_channel", "site"),
        ("public", "untouched", "site"),
    ]
    counts = {
        ("silver", "signals", "site"): 28_800_000,
        ("gold", "signal_daily", "site"): 4_200,
        ("webapp", "site_catalog_channel", "site"): 17,
        ("public", "untouched", "site"): 0,
    }
    connection = _Connection(columns, counts)
    from axiom.infra import db

    monkeypatch.setattr(db, "engine_for", lambda _name: _Engine(connection))
    return connection


def _run(params):
    return site_rename.run(params, ctx=None)


class TestItCountsBeforeItChangesAnything:
    def test_a_dry_run_writes_nothing(self, platform):
        _run({"old": "old-id", "new": "new-id"})
        assert platform.updates == []

    def test_it_reports_what_it_would_change(self, platform):
        result = _run({"old": "old-id", "new": "new-id"})
        assert result.value["rows"] == 28_804_217
        assert any("would rename" in line for line in result.actions_taken)

    def test_it_names_each_table(self, platform):
        result = _run({"old": "old-id", "new": "new-id"})
        tables = {(t["schema"], t["table"]) for t in result.value["tables"]}
        assert ("silver", "signals") in tables
        assert ("gold", "signal_daily") in tables

    def test_a_table_with_no_matching_rows_is_not_listed(self, platform):
        """Listing every table with a site column would bury the two that
        matter under the ones that do not."""
        result = _run({"old": "old-id", "new": "new-id"})
        assert ("public", "untouched") not in {
            (t["schema"], t["table"]) for t in result.value["tables"]
        }

    def test_it_says_how_to_actually_do_it(self, platform):
        result = _run({"old": "old-id", "new": "new-id"})
        assert any("apply=true" in line for line in result.actions_taken)

    def test_a_read_only_pass_does_not_hold_a_transaction_open(self, platform):
        """A read transaction left open on a 28M-row table is a lock
        somebody else waits behind."""
        _run({"old": "old-id", "new": "new-id"})
        assert platform.rolled_back


class TestApplying:
    def test_it_updates_every_table_that_matched(self, platform):
        _run({"old": "old-id", "new": "new-id", "apply": True})
        assert len(platform.updates) == 3

    def test_it_passes_both_ids_as_parameters(self, platform):
        """Interpolating a site id into the statement would make a site
        named `'; --` a migration that rewrites the wrong rows."""
        _run({"old": "old-id", "new": "new-id", "apply": True})
        for _sql, params in platform.updates:
            assert params == {"old": "old-id", "new": "new-id"}

    def test_it_reports_the_rows_it_wrote(self, platform):
        result = _run({"old": "old-id", "new": "new-id", "apply": True})
        written = sum(t["updated"] for t in result.value["tables"])
        assert written == 28_804_217

    def test_the_result_says_it_was_applied(self, platform):
        assert _run({"old": "old-id", "new": "new-id", "apply": True}).value["applied"]


class TestItRefusesWhatCannotBeMeant:
    @pytest.mark.parametrize("params", [{}, {"old": "a"}, {"new": "b"}, {"old": "  "}])
    def test_both_ids_are_required(self, platform, params):
        assert not _run(params).ok

    def test_renaming_an_id_to_itself_is_refused(self, platform):
        """Not an error worth running: it would count every row and change
        none, and read as a successful migration."""
        result = _run({"old": "same", "new": "same"})
        assert not result.ok
        assert "same" in result.value["error"]

    def test_an_unreachable_platform_says_so(self, monkeypatch):
        from axiom.infra import db

        def _boom(_name):
            raise RuntimeError("no such database")

        monkeypatch.setattr(db, "engine_for", _boom)
        result = _run({"old": "a", "new": "b"})
        assert not result.ok
        assert "no such database" in result.value["error"]


class TestNothingToDo:
    def test_a_site_with_no_rows_says_so_plainly(self, platform):
        """Rather than reporting a successful rename of nothing, which is
        how somebody concludes a migration ran when it found no data."""
        result = _run({"old": "never-used", "new": "new-id"})
        assert result.ok
        assert any("nothing to rename" in line for line in result.actions_taken)
        assert result.value["rows"] == 0


class TestACollisionIsRefusedRatherThanResolved:
    """A forked site has rows under BOTH ids, so the same key can exist
    twice — on the node three channels existed under both FANGIO ids. A bulk
    UPDATE then fails partway and leaves the site half-renamed, which is
    the state this tool exists to end.
    """

    @pytest.fixture
    def colliding(self, monkeypatch):
        connection = _Connection(
            [("webapp", "site_catalog_channel", "site")],
            {("webapp", "site_catalog_channel", "site"): 45},
            unique_keys=[("site",), ("feed",), ("channel",)],
            collisions=3,
        )
        from axiom.infra import db

        monkeypatch.setattr(db, "engine_for", lambda _n: _Engine(connection))
        return connection

    def test_it_refuses(self, colliding):
        assert not _run({"old": "old-id", "new": "new-id", "apply": True}).ok

    def test_it_writes_nothing(self, colliding):
        _run({"old": "old-id", "new": "new-id", "apply": True})
        assert colliding.updates == []

    def test_it_names_the_table_and_the_count(self, colliding):
        result = _run({"old": "old-id", "new": "new-id", "apply": True})
        joined = " ".join(result.actions_taken)
        assert "site_catalog_channel" in joined
        assert "(3)" in joined

    def test_it_says_the_choice_is_not_its_to_make(self, colliding):
        """Which row wins — the one with the history or the one the new id
        has been collecting — is a judgement about the data."""
        result = _run({"old": "old-id", "new": "new-id", "apply": True})
        assert any("judgement about the data" in line for line in result.actions_taken)

    def test_a_dry_run_reports_it_too(self, colliding):
        """Finding this only when applying would mean discovering it at
        twenty-eight million rows."""
        assert not _run({"old": "old-id", "new": "new-id"}).ok
