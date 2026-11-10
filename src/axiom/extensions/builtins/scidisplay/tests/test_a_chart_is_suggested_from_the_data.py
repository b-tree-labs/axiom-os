# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Chart suggestions (ADR-216): the compatibility matrix, the ranking, and
that every suggested spec draws through the real renderer."""

from __future__ import annotations

import json
import math
import random

import pytest

from axiom.extensions.builtins.scidisplay import suggest as sg
from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec
from axiom.extensions.builtins.scidisplay.unified_spec import validate

T = [f"2026-03-{1 + i // 24:02d}T{i % 24:02d}:00:00Z" for i in range(168)]


def _wave(n: int, base: float, amp: float, period: float) -> list[float]:
    return [base + amp * math.sin(i / period) for i in range(n)]


# Fixture results, one per profile the table distinguishes. Demo-free nouns.
FIXTURES: dict[str, object] = {
    "time-1unit-2series": {"series": [
        {"name": "inlet", "unit": "degC", "x": T, "y": _wave(168, 40, 5, 7)},
        {"name": "outlet", "unit": "degC", "x": T, "y": _wave(168, 50, 6, 9)}]},
    "time-2units": {"series": [
        {"name": "temperature", "unit": "degC", "x": T, "y": _wave(168, 40, 5, 7)},
        {"name": "flow", "unit": "kg/s", "x": T, "y": _wave(168, 3, 1, 5)}]},
    "time-3units": {"series": [
        {"name": "a", "unit": "degC", "x": T, "y": _wave(168, 40, 5, 7)},
        {"name": "b", "unit": "kg/s", "x": T, "y": _wave(168, 3, 1, 5)},
        {"name": "c", "unit": "Pa", "x": T, "y": _wave(168, 1e5, 10, 5)}]},
    "time-amount-4series": {"series": [
        {"name": f"line {k}", "unit": "kWh", "x": T, "y": _wave(168, 10 + k, 2, 6 + k)} for k in range(4)]},
    "time-short": {"series": [{"name": "x", "unit": "m", "x": T[:6], "y": [1, 2, 3, 2, 1, 2]}]},
    "time-negative": {"series": [{"name": "balance", "unit": "kWh", "x": T, "y": _wave(168, 0, 5, 7)}]},
    "categories-4": {"rows": [{"group": g, "rows": n} for g, n in
                              (("north", 104), ("south", 27), ("east", 25), ("west", 8))]},
    "categories-12": {"rows": [{"group": f"g{i}", "count": 10 + i} for i in range(12)]},
    "categories-negative": {"rows": [{"group": g, "delta": d} for g, d in (("a", 3), ("b", -2), ("c", 5))]},
    "categories-intensive": {"rows": [{"room": r, "temp": t} for r, t in (("a", 21.5), ("b", 19.0), ("c", 23.1))],
                             "units": {"temp": "degC"}},
    "value-bare": {"value": 42.0, "unit": "kg"},
    "value-limit": {"value": 870.0, "unit": "kW", "max": 1000.0, "label": "peak"},
    "value-percent": {"value": 111.0, "unit": "percent", "label": "peak share of rated"},
    "pairs": {"rows": [{"x": i, "y": 2 * i + 1} for i in range(30)]},
    "values-only": {"rows": [{"v": float(i % 17)} for i in range(50)]},
    "grid": {"rows": [{"day": d, "hour": h, "load": float((len(d) * int(h)) % 7 + 1)} for d in ("mon", "tues", "wednes")
                      for h in ("00", "06", "12", "18")]},
    "hierarchy": {"name": "storage", "unit": "rows", "children": [
        {"name": "feed a", "children": [{"name": "x", "value": 30}, {"name": "y", "value": 12}]},
        {"name": "feed b", "children": [{"name": "x", "value": 8}, {"name": "z", "value": 20}]}]},
    "state": {"rows": [{"t": T[i], "mode": ("idle", "run", "hold")[i // 20 % 3]} for i in range(80)]},
}

# The table, as written in ADR-216: for each fixture, the chips that may draw it.
# Everything not listed must be refused, with a reason.
VALID: dict[str, set[str]] = {
    "time-1unit-2series": {"line", "step", "sparkline", "kpi", "histogram", "box", "scatter"},
    "time-2units": {"line-dual", "line-panels", "sparkline", "kpi", "scatter"},
    "time-3units": {"line-panels", "sparkline", "kpi"},
    "time-amount-4series": {"line", "area", "area-stacked", "step", "sparkline", "kpi", "histogram", "box",
                            "heatmap"},
    "time-short": {"line", "area", "step", "sparkline", "kpi", "bar", "box"},
    "time-negative": {"line", "step", "sparkline", "kpi", "histogram", "box"},
    "categories-4": {"bar", "bar-horizontal", "pie", "donut"},
    "categories-12": {"bar", "bar-horizontal"},
    "categories-negative": {"bar", "bar-horizontal"},
    "categories-intensive": {"bar", "bar-horizontal"},
    "value-bare": set(),
    "value-limit": {"gauge", "bullet", "progress", "kpi"},
    "value-percent": {"gauge", "bullet", "progress", "kpi"},
    "pairs": {"line", "area", "scatter", "histogram", "box"},
    "values-only": {"histogram", "box"},
    "grid": {"heatmap", "bar", "bar-stacked"},
    "hierarchy": {"bar", "bar-horizontal", "bar-stacked", "pie", "donut", "flame"},
    "state": {"state", "step"},
}


def _chips() -> tuple[str, ...]:
    return tuple(sg.RULES)


@pytest.mark.parametrize("fixture", sorted(FIXTURES))
@pytest.mark.parametrize("chip", _chips())
def test_the_compatibility_matrix(fixture: str, chip: str) -> None:
    p = sg.profile(sg.normalise(FIXTURES[fixture]))
    [verdict] = sg.compatible(p, chips=[chip])
    expected = chip in VALID[fixture]
    assert verdict.valid is expected, f"{chip} on {fixture}: {verdict.reason}"
    assert verdict.reason, "every verdict says why, valid or not"


def test_every_kind_the_renderer_registers_has_a_rule() -> None:
    from axiom.extensions.builtins.scidisplay.unified_spec import registered_kinds

    ruled = {sg.chip_kind(c) for c in sg.RULES}
    assert set(registered_kinds()) <= ruled, set(registered_kinds()) - ruled


def test_a_kind_nothing_draws_is_not_suggested() -> None:
    from axiom.extensions.builtins.scidisplay.unified_spec import registered_kinds

    s = sg.suggest(FIXTURES["hierarchy"], "where does it go")
    drawn = set(registered_kinds())
    assert all(x.kind in drawn for x in s.all())
    if "flame" not in drawn:
        assert "flame" not in {x.chip for x in s.all()}


@pytest.mark.parametrize("fixture", sorted(FIXTURES))
def test_every_suggested_spec_is_valid_and_draws(fixture: str) -> None:
    s = sg.suggest(FIXTURES[fixture], "")
    for x in s.all():
        assert validate(x.spec) == [], (x.chip, validate(x.spec))
        out = render_spec(x.spec, width=640)
        assert out.svg.startswith("<svg"), x.chip
        assert "no series to draw" not in out.svg and "no readings" not in out.svg, x.chip


def _random_result(rng: random.Random) -> object:
    shape = rng.choice(["series", "rows-cat", "rows-pairs", "value", "tree"])
    if shape == "series":
        n = rng.randint(1, 300)
        k = rng.randint(1, 5)
        units = rng.sample(["degC", "kWh", "W", "percent", "m", ""], rng.randint(1, 3))
        return {"series": [{"name": f"s{i}", "unit": rng.choice(units), "x": T[:min(n, len(T))],
                            "y": [rng.choice([None, rng.uniform(-50, 500)]) if rng.random() < 0.05
                                  else rng.uniform(-50 if rng.random() < 0.2 else 0, 500)
                                  for _ in range(min(n, len(T)))]} for i in range(k)]}
    if shape == "rows-cat":
        k = rng.randint(1, 50)
        return {"rows": [{"g": f"c{i}", "v": rng.uniform(-5 if rng.random() < 0.2 else 0.1, 99)}
                         for i in range(k)]}
    if shape == "rows-pairs":
        return {"rows": [{"a": rng.uniform(0, 9), "b": rng.uniform(0, 9)} for _ in range(rng.randint(1, 80))]}
    if shape == "value":
        out: dict[str, object] = {"value": rng.uniform(0, 200), "unit": rng.choice(["W", "percent", "kg"])}
        if rng.random() < 0.5:
            out["max"] = 250.0
        return out
    return {"name": "t", "children": [{"name": f"p{i}", "children": [
        {"name": f"q{j}", "value": rng.uniform(0, 9)} for j in range(rng.randint(0, 4))]}
        for i in range(rng.randint(1, 6))]}


def test_every_suggestion_for_any_result_draws_without_error() -> None:
    """The property: whatever the data, whatever is suggested renders."""
    rng = random.Random(216)
    drawn = 0
    for _ in range(250):
        result = _random_result(rng)
        try:
            s = sg.suggest(result, rng.choice(["", "trend", "compare a and b", "which is most", "spread"]))
        except sg.SuggestError:
            continue
        for x in s.all():
            assert validate(x.spec) == [], (x.chip, validate(x.spec), json.dumps(result)[:300])
            render_spec(x.spec, width=480)
            drawn += 1
    assert drawn > 300


@pytest.mark.parametrize(("fixture", "question", "top"), [
    ("time-1unit-2series", "how did the temperature behave last week", ["line", "box", "histogram"]),
    ("categories-4", "which group has the most rows", ["bar-horizontal", "pie"]),
    ("value-percent", "how close did we get to rated", ["gauge", "bullet", "progress"]),
    ("time-2units", "compare temperature and flow", ["line-dual", "scatter"]),
    ("hierarchy", "where does the storage go", ["flame", "bar-stacked", "pie"]),
    ("values-only", "", ["histogram", "box"]),
])
def test_the_ranking_for_a_question(fixture: str, question: str, top: list[str]) -> None:
    s = sg.suggest(FIXTURES[fixture], question)
    got = [x.chip for x in s.suggestions]
    assert got[: len(top)] == top, (got, s.intents)
    assert 1 <= len(got) <= sg.TOP
    assert all(x.reason for x in s.suggestions)


def test_the_ranking_is_deterministic() -> None:
    a = sg.to_block(sg.suggest(FIXTURES["time-2units"], "compare"))
    b = sg.to_block(sg.suggest(FIXTURES["time-2units"], "compare"))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_a_preference_reorders_only_within_the_valid_set() -> None:
    s = sg.suggest(FIXTURES["categories-4"], "which is most", prefer=["gauge", "pie"])
    assert s.suggestions[0].chip == "pie"
    assert "gauge" not in {x.chip for x in s.all()}
    assert s.ignored_preferences == ("gauge",)


def test_a_bare_scalar_is_not_charted() -> None:
    s = sg.suggest(FIXTURES["value-bare"], "what is it")
    assert not s and s.quiet


def test_switching_to_an_invalid_kind_says_why() -> None:
    why = sg.check_kind(FIXTURES["categories-12"], "pie")
    assert "12 parts" in why
    assert sg.check_kind(FIXTURES["time-1unit-2series"], "box") == ""
    assert "no chart kind" in sg.check_kind(FIXTURES["time-1unit-2series"], "radar")


def test_the_block_carries_each_dataset_once() -> None:
    block = sg.to_block(sg.suggest(FIXTURES["time-1unit-2series"], "trend"))
    keys = [e["data"] for e in [*block["suggestions"], *block["more"]]]
    assert len(set(keys)) < len(keys), "kinds drawing the same numbers share one dataset"
    for e in [*block["suggestions"], *block["more"]]:
        spec = sg.spec_of(block, e["id"])
        assert spec is not None and validate(spec) == []


def test_decimation_keeps_the_peak() -> None:
    n = 5000
    ys = [0.0] * n
    ys[3217] = 99.0
    xs = [f"2026-01-01T00:{i // 60 % 60:02d}:{i % 60:02d}Z" for i in range(n)]
    s = sg.suggest({"series": [{"name": "spike", "unit": "W", "x": xs, "y": ys}]}, "trend", width=400)
    line = next(x for x in s.all() if x.chip == "line")
    ys2 = line.spec["data"]["series"][0]["y"]
    assert len(ys2) <= 400 and max(ys2) == 99.0


def test_the_profile_reads_roles_from_values_not_names() -> None:
    p = sg.profile(sg.normalise({"rows": [{"ts": "not a time", "t0": T[i], "v": i} for i in range(5)]}))
    assert p.x_role == "time" and p.series_count >= 1
    doc = p.to_document()
    assert {"form", "fields", "rows", "series", "units", "sign"} <= set(doc)


def test_the_profile_states_cadence_sparsity_and_reference() -> None:
    ys: list[float | None] = [1.0, None, 3.0, 4.0]
    p = sg.profile(sg.normalise({"series": [{"name": "a", "unit": "percent", "x": T[:4], "y": ys}]}))
    assert p.cadence_s == 3600.0
    assert p.sparsity == pytest.approx(0.25)
    assert p.reference.get("target") == 100.0


def test_a_tables_chart_offer_comes_from_the_same_engine() -> None:
    """``tabulate`` → ``offer_for`` → ``suggest``: one engine for the picture."""
    from axiom.extensions.builtins.scidisplay.chart_choice import offer_for

    rows = [{"t": T[i], "channel": c, "value": float(i % 9 + k)} for i in range(40)
            for k, c in enumerate(("a", "b"))]
    offer = offer_for(rows)
    assert offer.kind == "timeseries"
    top = sg.suggest({"rows": rows}, "").suggestions[0].chip
    assert offer.suggested == top
    doc = offer.to_document()
    assert doc["suggested"] == top and doc["suggestions"][0]["id"] == top

    cats = [{"group": g, "rows": n} for g, n in (("a", 4), ("b", 9), ("c", 2))]
    offer = offer_for(cats)
    assert not offer.kind, "no time axis to read back from the store"
    assert offer.suggested in ("bar", "bar-horizontal"), "and still a bar chart"
