# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A declaration must be able to reach the history it should have had.

The conform insert was `ON CONFLICT (row_hash, channel) DO NOTHING`, and
`row_hash` hashes the SOURCE ROW — which does not change when a channel map
does. So a unit filled in later, a fault code declared later, a role
corrected later could never reach a row already written. A correction applied
only to future data, and the 544 GB of bronze retained precisely so we could
re-derive was unreachable.
"""

from __future__ import annotations

import re

from ..conformance import (
    _INSERT_CONFLICT,
    _REDERIVE_CONFLICT,
    REDERIVABLE_COLUMNS,
)


class TestTheDefaultStaysCheap:
    def test_an_ordinary_pass_still_skips_what_is_there(self):
        """A conform pass re-reads bronze it has already seen every time it
        runs. Making every run an UPDATE would rewrite the whole table
        nightly."""
        assert _INSERT_CONFLICT == "ON CONFLICT (row_hash, channel) DO NOTHING"

    def test_pg_upsert_defaults_to_it(self):
        import inspect

        from ..conformance import pg_upsert

        assert inspect.signature(pg_upsert).parameters["rederive"].default is False


class TestWhatARederiveMayChange:
    def test_it_changes_the_fields_computed_from_a_declaration(self):
        for column in ("unit", "quality", "role", "derivation"):
            assert column in REDERIVABLE_COLUMNS

    def test_value_is_included_because_a_verdict_withholds_it(self):
        """ADR-132 D3: a `bad` reading has value NULL. That makes a re-derive
        able to change a number, which is why the skill is dry-run by
        default."""
        assert "value" in REDERIVABLE_COLUMNS

    def test_it_never_rewrites_the_row_s_identity(self):
        """If a re-derive produced a different site or instant it would not be
        the same reading, and rewriting them would mask a normalizer change
        rather than apply a declaration."""
        for column in ("site", "feed", "channel", "ts", "row_hash"):
            assert column not in REDERIVABLE_COLUMNS


class TestTheGuardThatMakesItAffordable:
    def test_it_only_writes_rows_that_actually_differ(self):
        """Postgres writes a new tuple for every row an UPDATE touches, even
        one set to the value it already held. Without this, re-deriving a
        17-million-row site rewrites all 17 million."""
        assert "IS DISTINCT FROM" in _REDERIVE_CONFLICT

    def test_it_uses_is_distinct_from_rather_than_inequality(self):
        """NULL is a real state here — an absent unit, a withheld value — and
        `NULL <> NULL` is NULL, which would skip exactly the rows this exists
        to fix."""
        assert not re.search(r"\)\s*<>\s*\(", _REDERIVE_CONFLICT)

    def test_every_rederivable_column_is_in_both_the_set_and_the_guard(self):
        """A column updated but left out of the guard would make the guard
        pass when that column alone changed; one in the guard but not the SET
        would report a change it never made."""
        for column in REDERIVABLE_COLUMNS:
            assert f"{column} = excluded.{column}" in _REDERIVE_CONFLICT
            assert f"silver.signals.{column}" in _REDERIVE_CONFLICT
            assert f"excluded.{column}" in _REDERIVE_CONFLICT

    def test_it_conflicts_on_the_same_key_as_the_default(self):
        assert "ON CONFLICT (row_hash, channel)" in _REDERIVE_CONFLICT


class TestTheRunnerReportsWhatChangedNotWhatItRead:
    def test_the_counter_distinguishes_them(self):
        """"17 million rows re-derived" is not an answer; "4,812 rows changed"
        is."""
        from ..conformance.runner import _counting_upsert

        class _Cur:
            rowcount = 0

        cur = _Cur()
        seen = []
        upsert, counts = _counting_upsert(lambda row: seen.append(row), cur)

        cur.rowcount = 1
        upsert({"a": 1})
        cur.rowcount = 0
        upsert({"a": 2})
        upsert({"a": 3})

        assert counts["seen"] == 3
        assert counts["changed"] == 1
        assert len(seen) == 3


class TestTheSkill:
    def test_it_is_a_cli_verb(self):
        from ..skills import verbs

        assert "rederive" in verbs()

    def test_the_cli_defaults_to_counting_not_writing(self):
        from ..cli import _parser

        args = _parser().parse_args(["rederive", "--site", "site-a"])
        assert args.apply is False
        assert args.site == "site-a"

    def test_apply_is_an_explicit_gesture(self):
        from ..cli import _parser

        assert _parser().parse_args(["rederive", "--apply"]).apply is True

    def test_it_registers_with_a_spec(self):
        from axiom.infra.skills import SkillRegistry

        from ..skills import bind

        registry = SkillRegistry()
        bind(registry)
        assert registry.spec("data.rederive").description

    def test_it_parses_a_comma_separated_site_list(self):
        from ..skills.rederive import _sites

        assert _sites({"site": "a, b ,c"}) == frozenset({"a", "b", "c"})

    def test_no_site_means_every_site(self):
        from ..skills.rederive import _sites

        assert _sites({}) is None
        assert _sites({"site": ""}) is None

    def test_blank_entries_do_not_become_a_site_named_nothing(self):
        from ..skills.rederive import _sites

        assert _sites({"site": "a,,  ,b"}) == frozenset({"a", "b"})


class _Cursor:
    def __init__(self):
        self.statements = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.statements.append(str(sql))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self):
        self.cur = _Cursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _run(tmp_path, monkeypatch, **kwargs):
    """Drive the real runner against an injected connection."""
    from ..conformance import runner as runner_mod

    conn = _Conn()
    monkeypatch.setattr(
        runner_mod, "resolve_site_map", lambda _state: ({"conn-a": "site-a"}, [])
    )

    class _Registry:
        def get(self, ref):
            return None

    stats = runner_mod.run_conform(
        bronze_root=tmp_path,
        dsn="postgres://x",
        connect=lambda _dsn: conn,
        registry=_Registry(),
        **kwargs,
    )
    return conn, stats


class TestTheDryRunReallyRollsBack:
    def test_apply_false_rolls_back_and_does_not_commit(self, tmp_path, monkeypatch):
        """Not a prediction: everything is executed, then undone, so the
        count is what would actually have happened."""
        conn, stats = _run(tmp_path, monkeypatch, rederive=True, apply=False)
        assert conn.rolled_back
        assert not conn.committed
        assert stats["applied"] is False
        assert conn.closed

    def test_apply_true_commits(self, tmp_path, monkeypatch):
        conn, stats = _run(tmp_path, monkeypatch, rederive=True, apply=True)
        assert conn.committed
        assert not conn.rolled_back
        assert stats["applied"] is True

    def test_the_connection_is_closed_either_way(self, tmp_path, monkeypatch):
        conn, _ = _run(tmp_path, monkeypatch, rederive=True, apply=False)
        assert conn.closed


class TestScoping:
    def test_a_named_site_that_maps_to_nothing_is_reported(self, tmp_path, monkeypatch):
        """Silence would read as "nothing needed changing", which is a
        different statement from "that site is not mapped to a connector"."""
        _, stats = _run(
            tmp_path, monkeypatch, rederive=True, apply=False,
            only_sites=frozenset({"site-that-does-not-exist"}),
        )
        assert stats["sites_not_matched"] == ["site-that-does-not-exist"]

    def test_a_site_that_does_map_is_not_reported_as_missing(self, tmp_path, monkeypatch):
        _, stats = _run(
            tmp_path, monkeypatch, rederive=True, apply=False,
            only_sites=frozenset({"site-a"}),
        )
        assert stats["sites_not_matched"] == []

    def test_no_scope_means_every_site(self, tmp_path, monkeypatch):
        _, stats = _run(tmp_path, monkeypatch, rederive=True, apply=False)
        assert stats["sites_not_matched"] == []


class TestTheFunnelSaysWhichModeItRanIn:
    def test_rederive_is_reported(self, tmp_path, monkeypatch):
        _, stats = _run(tmp_path, monkeypatch, rederive=True, apply=False)
        assert stats["rederive"] is True

    def test_an_ordinary_pass_says_so_too(self, tmp_path, monkeypatch):
        """A run that did NOT re-derive must not read like one that did."""
        _, stats = _run(tmp_path, monkeypatch, apply=True)
        assert stats["rederive"] is False
        assert stats["rows_changed"] == 0
