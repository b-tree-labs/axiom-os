# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""MVP-18-A1/A2, MVP-03-A4: the figure the Charts page reads carries its marked spans
and each point's quality, and draws held/stale/absent data as a neutral "—" hatch.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from axiom.extensions.builtins.data_platform.marks import MARKS
from axiom.extensions.builtins.scidisplay.chart_marks import TONE_LITERALS, draw_under
from axiom.extensions.builtins.scidisplay.node_figure_source import NodeFigureSource

from .test_a_figure_is_drawn_from_a_nodes_gold import T0, FakeDoor


class MarkedDoor(FakeDoor):
    """Buckets 10-12 of T1 withheld (value None, verdict bad); bucket 3 stale; 30-40 missing."""

    def _query_once(self, verb, params):  # noqa: ANN001
        out = super()._query_once(verb, params)
        if verb != "data.series":
            return out
        for group in out["data"]["series"]:
            if group["key"]["channel"] != "T1":
                continue
            kept = []
            for i, p in enumerate(group["points"]):
                if 30 <= i < 40:
                    continue
                if 10 <= i <= 12:
                    p = {**p, "value": None, "quality": "bad"}
                elif i == 3:
                    p = {**p, "quality": "stale"}
                elif i == 0:
                    p = {**p, "quality": "good"}
                kept.append(p)
            group["points"] = kept
        return out


@pytest.fixture
def fig(tmp_path):
    source = NodeFigureSource(MarkedDoor(), dates_cache_path=str(tmp_path / "days.json"))
    return source.figure("site-a", ["loop:T1"], t_from=T0, t_to=T0 + timedelta(hours=2))


def _bucket(fig) -> timedelta:
    n, unit = fig["served"]["bucket"].split()
    return timedelta(seconds=int(n) * {"second": 1, "minute": 60, "hour": 3600}[unit.rstrip("s")])


def test_mvp_18_a1_the_figure_carries_its_held_span(fig) -> None:
    held = [m for m in fig["marks"] if m["mark"] == "held"]
    step = _bucket(fig)
    assert held == [
        {
            "mark": "held",
            "start": (T0 + 10 * step).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": (T0 + 13 * step).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reason": "value withheld by a quality verdict",
            "series": "T1",
        }
    ]
    for m in fig["marks"]:
        assert m["mark"] in MARKS and set(m) == {"mark", "start", "end", "reason", "series"}


def test_mvp_18_a1_stale_and_gap_spans_are_marked_too(fig) -> None:
    kinds = {m["mark"] for m in fig["marks"]}
    assert {"held", "stale", "gap"} <= kinds
    [gap] = [m for m in fig["marks"] if m["mark"] == "gap"]
    step = _bucket(fig)
    assert gap["start"] == (T0 + 30 * step).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_mvp_18_a2_every_readout_point_has_its_quality_aligned(fig) -> None:
    [series] = fig["readout"]["series"]
    assert len(series["quality"]) == len(series["points"])
    assert series["quality"][0] == "good"
    assert series["quality"][3] == "stale"
    assert series["quality"][1] is None, "not reported is null, never defaulted to good"


def test_mvp_03_a4_a_held_span_is_drawn_as_a_neutral_hatch_labelled_dash(fig) -> None:
    svg = fig["svg"]
    assert 'class="axk-hatch"' in svg and "url(#axk-hatch)" in svg
    assert "—" in svg
    assert svg.count('<pattern id="axk-hatch"') == 1
    hatch = svg[svg.index('<pattern id="axk-hatch"') : svg.index("</pattern>")]
    assert TONE_LITERALS["neutral"] in hatch
    for status in ("warn", "alarm", "ok"):
        assert TONE_LITERALS[status] not in hatch


def test_mvp_03_a4_the_hatch_kind_draws_from_to_on_the_time_axis() -> None:
    def x_of(v):
        return float(v) if isinstance(v, (int, float)) else None

    out = draw_under(
        [{"type": "hatch", "from": 10, "to": 30, "label": "—"}],
        box=(0, 0, 100, 50),
        x_of=x_of,
        y_of=lambda v, a: None,
        label_font="sans",
    )
    rect = next(s for s in out if 'class="axk-hatch"' in s)
    assert 'x="10.0"' in rect and 'width="20.0"' in rect
    assert any("—" in s for s in out)


def test_mvp_18_a2_the_gold_series_reports_each_buckets_worst_quality() -> None:
    from axiom.extensions.builtins.data_platform import gold_query as gq
    from axiom.extensions.builtins.data_platform.company import QUALITY

    assert set(gq._QUALITY_RANK) == set(QUALITY)

    class Cur:
        def __init__(self) -> None:
            self.sql: list[str] = []
            self._rows: list = []

        def execute(self, sql, params=None):  # noqa: ANN001
            self.sql.append(sql)
            if "information_schema" in sql:
                self._rows = [
                    ("ts", "timestamptz"),
                    ("value", "double precision"),
                    ("quality", "text"),
                ]
            elif "date_bin" in sql:
                t = datetime(2026, 10, 30)
                self._rows = [(t, 1.0, 3, 4), (t + timedelta(minutes=1), 2.0, 1, None)]
            else:
                self._rows = []

        def fetchall(self):
            return self._rows

    cur = Cur()
    out = gq.series(cur, table="signals", column="value", bucket="1 minute", time_column="ts")
    points = out["data"]["series"]
    assert [p["quality"] for p in points] == ["bad", None]
    assert any("WHEN 'bad' THEN 4" in s for s in cur.sql)
