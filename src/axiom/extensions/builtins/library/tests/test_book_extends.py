# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-213 D11: a product Book extends the generic Data Book instead of replacing it."""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library.book import service as book
from axiom.extensions.builtins.library.book.place import Signals, place
from axiom.extensions.builtins.library.book.scheme import (
    SchemeError,
    compose,
    load_scheme_file,
    scheme_from_dict,
)

GENERIC = load_scheme_file(Path(book.__file__).with_name("data-book.toml"))

WIDGET = {
    "scheme": {"name": "widget", "display_name": "Widget Book", "extends": "data"},
    "categories": [
        {"path": "widget-logs", "label": "Widget Logs"},
        {"path": "reports", "label": "Reports"},
        {"path": "datasets/widget-runs", "label": "Widget runs"},
    ],
    "rules": [
        {"category": "widget-logs", "confidence": 0.9, "because": "it is a widget log",
         "when": {"extractor": ["pack.extract.widget_log"]}},
        {"category": "reports", "confidence": 0.62, "because": "it reads as a report",
         "when": {"text_any": ["executive summary"]}},
        {"category": "datasets/widget-runs", "confidence": 0.8, "because": "a widget run table",
         "when": {"name": ["run_*.csv"]}},
    ],
}


def _composed() -> object:
    return compose(scheme_from_dict(WIDGET), {"data": GENERIC})


def test_the_product_keeps_its_categories_first_and_gains_the_generic_ones():
    s = _composed()
    paths = [c.path for c in s.categories]
    assert paths[:2] == ["widget-logs", "reports"]
    assert {"images", "archives", "spreadsheets", "datasets", "datasets/widget-runs"} <= set(paths)
    # A parent always precedes its child, whichever scheme declared it.
    assert paths.index("datasets") < paths.index("datasets/widget-runs")
    assert s.lineage == ("widget", "data") and s.describe()["extends"] == "data"


def test_what_the_product_says_nothing_about_files_under_the_generic_book():
    s = _composed()
    assert place(Signals(name="map.png", extractors=["core.extract.image"]), s).path == "images"
    assert place(Signals(name="batch.zip", extractors=["core.extract.archive"]), s).path == "archives"
    assert place(Signals(name="cfg.json", extractors=["core.extract.structured"]), s).path == "structured"


def test_specific_beats_generic_on_ties():
    s = _composed()
    # Both match: the generic "documents" (0.70) and the product's "reports" (0.62).
    p = place(Signals(name="q3.pdf", extractors=["core.extract.pdf"], text="Executive summary\n..."), s)
    assert p.path == "reports" and p.alternatives[0].path == "documents"
    # A generic rule surer by more than the margin still wins: specific only breaks ties.
    loose = compose(scheme_from_dict({**WIDGET, "scheme": {**WIDGET["scheme"], "specific_margin": 0.0}}), {"data": GENERIC})
    assert place(Signals(name="q3.pdf", extractors=["core.extract.pdf"], text="Executive summary"), loose).path == "documents"


def test_a_product_rule_can_file_under_a_generic_category_it_does_not_redeclare():
    data = {**WIDGET, "rules": [*WIDGET["rules"], {"category": "images", "confidence": 0.9, "because": "a widget photo",
                                                   "when": {"name": ["widget_*.jpg"]}}]}
    s = compose(scheme_from_dict(data), {"data": GENERIC})
    assert place(Signals(name="widget_7.jpg"), s).path == "images"


def test_a_rule_for_an_undeclared_category_is_still_refused_after_composition():
    data = {**WIDGET, "rules": [{"category": "nowhere", "confidence": 0.9, "because": "x", "when": {"name": ["*"]}}]}
    with pytest.raises(SchemeError, match="nowhere"):
        compose(scheme_from_dict(data), {"data": GENERIC})


def test_a_missing_base_or_a_cycle_is_refused():
    with pytest.raises(SchemeError, match="not installed"):
        compose(scheme_from_dict(WIDGET), {})
    a = scheme_from_dict({"scheme": {"name": "a", "extends": "b"}, "categories": [{"path": "x", "label": "X"}]})
    b = scheme_from_dict({"scheme": {"name": "b", "extends": "a"}, "categories": [{"path": "y", "label": "Y"}]})
    with pytest.raises(SchemeError, match="cycle"):
        compose(a, {"a": a, "b": b})


def test_the_installed_product_book_is_served_composed():
    contrib.reset(rediscover=False)
    try:
        contrib.add("book_schemes", WIDGET, source="widget-pack")
        active = book.active_scheme()
        assert active.name == "widget" and "images" in active.by_path
    finally:
        contrib.reset()
