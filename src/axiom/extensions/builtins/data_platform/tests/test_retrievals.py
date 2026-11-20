# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A named retrieval: what to fetch, said once, reusable everywhere.

The problem it solves, found by walking a real node as a scoped researcher:
the same concept is spelled differently on every surface. The chart API takes
`t_from`/`t_to`; the telemetry API takes `start`/`end`; and FastAPI silently
ignores the ones it does not know, so a caller who uses the wrong spelling gets
a confident answer to a different question. Retyping a retrieval per surface is
where that mistake lives. Naming it once removes the retyping.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.data_platform import retrievals


class TestWhatARetrievalIs:
    def test_it_names_the_data_and_the_window(self):
        r = retrievals.Retrieval(
            name="fuel-temp-operating",
            site="site-a",
            feed="reactor.console",
            channels=("fuel_temp_1", "fuel_temp_2"),
            window="operating",
        )
        assert r.name == "fuel-temp-operating"
        assert r.channels == ("fuel_temp_1", "fuel_temp_2")

    def test_a_name_is_a_slug_so_it_can_be_a_cli_arg_and_a_url_segment(self):
        with pytest.raises(ValueError, match="name"):
            retrievals.Retrieval(name="Fuel Temp!", site="s", feed="f", channels=("c",))

    def test_it_refuses_to_name_nothing(self):
        """A retrieval with no channels is a name for no data. Saving it means
        discovering at call time that the name was never a question."""
        with pytest.raises(ValueError, match="channel"):
            retrievals.Retrieval(name="empty", site="s", feed="f", channels=())


class TestTheWindowIsASpanNotTwoInstants:
    def test_a_span_stays_useful_as_the_record_grows(self):
        r = retrievals.Retrieval(name="last-day", site="s", feed="f", channels=("c",), window="24h")
        assert r.window == "24h"

    def test_two_instants_are_allowed_and_flagged_as_frozen(self):
        """Sometimes a specific day IS the question — a named incident. It is
        allowed, and it says so, because a reader who expects "recent" from a
        name like `yesterday` needs to know it means one fixed day forever."""
        r = retrievals.Retrieval(
            name="the-sept-10-run",
            site="s",
            feed="f",
            channels=("c",),
            window="2026-09-10T08:00:00Z/2026-09-10T15:30:00Z",
        )
        assert r.frozen is True

    def test_a_span_is_not_frozen(self):
        r = retrievals.Retrieval(name="n", site="s", feed="f", channels=("c",), window="7d")
        assert r.frozen is False

    def test_an_open_window_lets_one_name_serve_many_days(self):
        """The window is the part a caller most often varies. Leaving it empty
        means the retrieval names WHAT to fetch and the caller says WHEN."""
        r = retrievals.Retrieval(name="n", site="s", feed="f", channels=("c",), window="")
        assert r.open_window is True
        assert r.frozen is False


class TestTheCatalog:
    def test_a_saved_retrieval_comes_back(self, tmp_path):
        cat = retrievals.Catalog(tmp_path / "retrievals.json")
        cat.save(retrievals.Retrieval(name="a", site="s", feed="f", channels=("c",)))
        assert [r.name for r in cat.list()] == ["a"]
        assert cat.get("a").feed == "f"

    def test_saving_the_same_name_twice_needs_saying_so(self, tmp_path):
        cat = retrievals.Catalog(tmp_path / "r.json")
        cat.save(retrievals.Retrieval(name="a", site="s", feed="f", channels=("c",)))
        with pytest.raises(retrievals.NameTaken):
            cat.save(retrievals.Retrieval(name="a", site="s", feed="f", channels=("d",)))
        cat.save(retrievals.Retrieval(name="a", site="s", feed="f", channels=("d",)), replace=True)
        assert cat.get("a").channels == ("d",)

    def test_an_absent_name_says_which_names_exist(self, tmp_path):
        """A catalog that answers "not found" and nothing else makes the reader
        run a second command to find out what they should have typed."""
        cat = retrievals.Catalog(tmp_path / "r.json")
        cat.save(retrievals.Retrieval(name="fuel-temp", site="s", feed="f", channels=("c",)))
        with pytest.raises(KeyError, match="fuel-temp"):
            cat.get("fueltemp")

    def test_a_missing_catalog_is_empty_not_an_error(self, tmp_path):
        assert retrievals.Catalog(tmp_path / "nope.json").list() == []

    def test_removing_one_leaves_the_others(self, tmp_path):
        cat = retrievals.Catalog(tmp_path / "r.json")
        for n in ("a", "b"):
            cat.save(retrievals.Retrieval(name=n, site="s", feed="f", channels=("c",)))
        cat.remove("a")
        assert [r.name for r in cat.list()] == ["b"]


class TestARoundTripLosesNothing:
    def test_a_field_this_version_does_not_know_survives(self, tmp_path):
        """Forward compatibility, and the reason it matters: a field the loader
        drops is worse than a field that was never there, because the writer
        believes it was stored. Two versions of this tool will share one
        catalog file the moment anybody upgrades."""
        path = tmp_path / "r.json"
        path.write_text(
            json.dumps(
                {
                    "retrievals": [
                        {
                            "name": "a",
                            "site": "s",
                            "feed": "f",
                            "channels": ["c"],
                            "window": "24h",
                            "some_future_field": {"kind": "heatmap"},
                        }
                    ]
                }
            )
        )
        cat = retrievals.Catalog(path)
        cat.save(retrievals.Retrieval(name="b", site="s", feed="f", channels=("d",)))
        again = json.loads(path.read_text())
        kept = [r for r in again["retrievals"] if r["name"] == "a"][0]
        assert kept["some_future_field"] == {"kind": "heatmap"}

    def test_channels_survive_as_a_list_and_come_back_as_a_tuple(self, tmp_path):
        cat = retrievals.Catalog(tmp_path / "r.json")
        cat.save(retrievals.Retrieval(name="a", site="s", feed="f", channels=("x", "y")))
        assert retrievals.Catalog(tmp_path / "r.json").get("a").channels == ("x", "y")


class TestItSpeaksEverySurfacesDialect:
    """The point of the whole thing: one named retrieval, whatever the caller.

    Each surface spells the window differently and silently ignores what it
    does not recognise, so the translation has to happen HERE, once, rather
    than in each caller's head.
    """

    R = dict(name="n", site="site-a", feed="reactor.console", channels=("power",), window="24h")

    def test_the_chart_dialect_uses_t_from_and_t_to(self):
        q = retrievals.as_query(retrievals.Retrieval(**self.R), dialect="chart")
        assert q["feed"] == "reactor.console"
        assert q["channels"] == "power"
        assert "last" in q or "t_from" in q

    def test_the_telemetry_dialect_never_emits_the_charts_spelling(self):
        q = retrievals.as_query(retrievals.Retrieval(**self.R), dialect="telemetry")
        assert "t_from" not in q and "t_to" not in q

    def test_a_span_the_telemetry_api_cannot_express_is_REPORTED_not_dropped(self):
        """The telemetry API has no span parameter. Emitting `last=24h` would be
        silently ignored by it and the caller would get the whole record — the
        exact failure this module exists to end. So a span comes back under a
        key the caller must deal with rather than one the server will drop."""
        q = retrievals.as_query(retrievals.Retrieval(**self.R), dialect="telemetry")
        assert q.get("_span") == "24h"
        assert "last" not in q

    def test_an_unknown_dialect_is_refused_rather_than_guessed(self):
        with pytest.raises(ValueError, match="dialect"):
            retrievals.as_query(retrievals.Retrieval(**self.R), dialect="graphql")

    def test_a_frozen_window_becomes_instants_in_both_dialects(self):
        r = retrievals.Retrieval(
            name="n",
            site="s",
            feed="f",
            channels=("c",),
            window="2026-09-10T08:00:00Z/2026-09-10T15:30:00Z",
        )
        chart = retrievals.as_query(r, dialect="chart")
        tele = retrievals.as_query(r, dialect="telemetry")
        assert chart["t_from"].startswith("2026-09-10T08:00")
        assert tele["start"].startswith("2026-09-10T08:00")
