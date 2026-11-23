# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The retrieval verbs: name a question once, ask it from anywhere."""

from __future__ import annotations

from types import SimpleNamespace

from axiom.extensions.builtins.data_platform.skills import retrieval

BASE = {"site": "site-a", "feed": "reactor.console", "channels": ["power"]}


def ctx(tmp_path):
    return SimpleNamespace(state_dir=tmp_path)


class TestSavingAndReading:
    def test_a_saved_retrieval_comes_back(self, tmp_path):
        assert retrieval.save({"name": "fuel", **BASE, "window": "24h"}, ctx(tmp_path)).ok
        got = retrieval.show({"name": "fuel"}, ctx(tmp_path))
        assert got.ok and got.value["channels"] == ["power"]

    def test_a_bad_name_is_refused_with_the_reason(self, tmp_path):
        out = retrieval.save({"name": "Fuel Temp!", **BASE}, ctx(tmp_path))
        assert not out.ok and "CLI argument" in out.errors[0]

    def test_naming_no_channel_is_refused(self, tmp_path):
        out = retrieval.save({"name": "empty", "site": "s", "feed": "f"}, ctx(tmp_path))
        assert not out.ok and "names no data" in out.errors[0]

    def test_the_same_name_twice_needs_replace(self, tmp_path):
        retrieval.save({"name": "a", **BASE}, ctx(tmp_path))
        assert not retrieval.save({"name": "a", **BASE}, ctx(tmp_path)).ok
        assert retrieval.save({"name": "a", **BASE, "replace": True}, ctx(tmp_path)).ok

    def test_an_absent_name_says_what_the_catalogue_holds(self, tmp_path):
        retrieval.save({"name": "fuel-temp", **BASE}, ctx(tmp_path))
        out = retrieval.show({"name": "fueltemp"}, ctx(tmp_path))
        assert not out.ok and "fuel-temp" in out.errors[0]

    def test_removing_one_leaves_the_rest(self, tmp_path):
        for n in ("a", "b"):
            retrieval.save({"name": n, **BASE}, ctx(tmp_path))
        assert retrieval.remove({"name": "a"}, ctx(tmp_path)).ok
        got = retrieval.list_saved({}, ctx(tmp_path))
        assert [r["name"] for r in got.value["retrievals"]] == ["b"]

    def test_removing_what_is_not_there_is_an_error_not_a_silent_ok(self, tmp_path):
        assert not retrieval.remove({"name": "ghost"}, ctx(tmp_path)).ok


class TestItSaysWhenANameWillGoStale:
    def test_two_instants_are_flagged_at_save_time(self, tmp_path):
        """Said when it is written, not discovered later. A name like
        `yesterday` over two fixed instants is a lie the day after."""
        out = retrieval.save(
            {
                "name": "the-sept-10-run",
                **BASE,
                "window": "2026-09-10T08:00:00Z/2026-09-10T15:30:00Z",
            },
            ctx(tmp_path),
        )
        assert out.ok and out.value["frozen"] is True
        assert any("forever" in a for a in out.actions_taken)

    def test_a_span_is_not_flagged(self, tmp_path):
        out = retrieval.save({"name": "last-day", **BASE, "window": "24h"}, ctx(tmp_path))
        assert out.value["frozen"] is False
        assert not any("forever" in a for a in out.actions_taken)

    def test_an_open_window_is_reported_as_such(self, tmp_path):
        out = retrieval.save({"name": "any-day", **BASE}, ctx(tmp_path))
        assert out.value["open_window"] is True


class TestTheDialectVerb:
    def test_it_gives_the_parameters_that_surface_reads(self, tmp_path):
        retrieval.save({"name": "p", **BASE, "window": "24h"}, ctx(tmp_path))
        out = retrieval.dialect({"name": "p", "dialect": "chart"}, ctx(tmp_path))
        assert out.ok and out.value["query"]["last"] == "24h"

    def test_an_unknown_dialect_is_refused_rather_than_guessed(self, tmp_path):
        retrieval.save({"name": "p", **BASE}, ctx(tmp_path))
        out = retrieval.dialect({"name": "p", "dialect": "graphql"}, ctx(tmp_path))
        assert not out.ok and "graphql" in out.errors[0]

    def test_a_span_the_surface_cannot_express_is_reported_not_sent(self, tmp_path):
        """Emitting `last=24h` at a server with no span parameter is how a caller
        receives the whole record believing it asked for a day."""
        retrieval.save({"name": "p", **BASE, "window": "24h"}, ctx(tmp_path))
        out = retrieval.dialect({"name": "p", "dialect": "telemetry"}, ctx(tmp_path))
        assert out.ok
        assert "_span" in out.value["query"]
        assert any("no parameter for" in a for a in out.actions_taken)

    def test_it_names_the_dialects_it_knows(self, tmp_path):
        retrieval.save({"name": "p", **BASE}, ctx(tmp_path))
        out = retrieval.dialect({"name": "p", "dialect": "chart"}, ctx(tmp_path))
        assert set(out.value["dialects"]) == {"chart", "telemetry"}


class TestOneCatalogueForEverySurface:
    def test_the_state_dir_is_where_it_lives(self, tmp_path):
        """Two stores would mean an agent and a person disagreeing about what a
        name means, which is worse than having no catalogue."""
        retrieval.save({"name": "a", **BASE}, ctx(tmp_path))
        assert (tmp_path / "retrievals.json").exists()

    def test_an_explicit_catalog_path_wins_for_a_test(self, tmp_path):
        where = tmp_path / "elsewhere.json"
        retrieval.save({"name": "a", **BASE, "catalog": str(where)}, ctx(tmp_path))
        assert where.exists()
        assert not (tmp_path / "retrievals.json").exists()
