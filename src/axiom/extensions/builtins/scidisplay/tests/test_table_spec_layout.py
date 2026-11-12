# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Column layout in the spec: widths, resizing, pinning, type, and the table's id.

All optional, so a document from before these fields reads and writes back
unchanged; set, they reach the web component by its own names, so the spec,
the web table and the terminal agree on what a column is.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay.table_spec import (
    Column,
    SpecFormatError,
    TableSpec,
    UnknownFieldError,
    parse_document,
)


def _spec(**col):
    return TableSpec(kind="rows", title="Runs", columns=(Column(id="name", label="Name", **col),))


def test_a_document_without_layout_round_trips_unchanged():
    doc = _spec().to_document()
    assert set(doc["columns"][0]) == {"id", "label", "sortable", "align"}
    assert "table_id" not in doc
    assert parse_document(doc).to_document() == doc


def test_layout_round_trips_and_reaches_the_web_component_by_its_own_names():
    spec = TableSpec(
        kind="rows", title="Runs", table_id="library.files",
        columns=(Column(id="size", label="Size", width=96, min_width=64, max_width=200, resizable=False,
                        pinned="right", type="number"),),
    )
    doc = spec.to_document()
    assert parse_document(doc) == spec
    props = spec.to_datatable_props()
    assert props["tableId"] == "library.files"
    assert props["columns"][0] == {"id": "size", "label": "Size", "sortable": False, "align": "left", "width": 96,
                                   "minWidth": 64, "maxWidth": 200, "resizable": False, "pinned": "right",
                                   "type": "number"}


@pytest.mark.parametrize("col", [
    {"width": 0}, {"width": -5}, {"width": True}, {"min_width": 300, "max_width": 100},
    {"pinned": "top"}, {"type": "sparkle"}, {"resizable": "yes"},
])
def test_bad_layout_is_refused_by_name(col):
    with pytest.raises(SpecFormatError):
        _spec(**col)


def test_an_unknown_column_key_is_still_refused():
    doc = _spec().to_document()
    doc["columns"][0]["colour"] = "red"
    with pytest.raises(UnknownFieldError):
        parse_document(doc)


def test_a_table_id_must_be_an_identifier():
    with pytest.raises(SpecFormatError):
        TableSpec(kind="rows", title="Runs", table_id="has spaces", columns=(Column(id="a", label="A"),))
