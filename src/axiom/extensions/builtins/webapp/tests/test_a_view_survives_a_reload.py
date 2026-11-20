# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A surface a person configures and loses is one they configure once.

Choosing four channels across two feeds, setting a window and turning on a
comparison is real work. It should survive a reload, a laptop and a week.
"""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from axiom.extensions.builtins.webapp.catalog import models, store
from axiom.extensions.builtins.webapp.views import (
    delete_view,
    list_views,
    read_view,
    write_view,
)
from axiom.extensions.builtins.webapp.views.models import LAST, SavedView  # noqa: F401
from axiom.extensions.builtins.webapp.views.store import (
    MAX_DOCUMENT_BYTES,
    MAX_VIEWS_PER_SURFACE,
    TooManyViews,
    ViewTooLarge,
)


@pytest.fixture
def bound(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'v.db'}")
    models.Base.metadata.create_all(engine)
    s = Session(engine)

    @contextlib.contextmanager
    def _provider():
        yield s

    monkeypatch.setattr(store, "_provider", _provider)
    yield s
    s.close()


CHART = {
    "sites": ["site-a", "site-b"],
    "feeds": {"site-a": ["alpha", "beta"]},
    "picked": {"site-a": ["alpha:T1", "beta:T2"]},
    "window": {"from": "2026-01-01T00:00:00+00:00", "to": "2026-02-01T00:00:00+00:00"},
    "rebase": True,
    "layout": "2",
}


class TestItComesBack:
    def test_what_was_saved_is_what_is_read(self, bound):
        write_view("ben", "chart", CHART)
        assert read_view("ben", "chart") == CHART

    def test_a_surface_opening_for_the_first_time_is_not_an_error(self, bound):
        """It opens fresh. Nobody has configured it yet, which is the normal
        case rather than a failure."""
        assert read_view("nobody", "chart") is None

    def test_saving_again_replaces_rather_than_accumulates(self, bound):
        write_view("ben", "chart", CHART)
        write_view("ben", "chart", {"sites": ["site-c"]})
        assert read_view("ben", "chart") == {"sites": ["site-c"]}
        assert len(list_views("ben", "chart")) == 1

    def test_a_named_view_sits_beside_the_automatic_one(self, bound):
        write_view("ben", "chart", CHART)
        write_view("ben", "chart", {"sites": ["site-c"]}, name="startup")
        assert read_view("ben", "chart") == CHART
        assert read_view("ben", "chart", "startup") == {"sites": ["site-c"]}
        assert {v["name"] for v in list_views("ben", "chart")} == {LAST, "startup"}

    def test_a_view_can_be_forgotten(self, bound):
        write_view("ben", "chart", CHART, name="startup")
        assert delete_view("ben", "chart", "startup") is True
        assert delete_view("ben", "chart", "startup") is False


class TestItBelongsToSomebody:
    def test_two_people_do_not_share_one(self, bound):
        """Two people looking at one deployment are not looking at the same
        question."""
        write_view("ben", "chart", CHART)
        write_view("sam", "chart", {"sites": ["site-z"]})
        assert read_view("ben", "chart") == CHART
        assert read_view("sam", "chart") == {"sites": ["site-z"]}

    def test_nor_do_two_surfaces(self, bound):
        write_view("ben", "chart", CHART)
        write_view("ben", "table", {"columns": ["ts"]})
        assert read_view("ben", "chart") == CHART

    def test_a_view_with_no_owner_is_refused(self, bound):
        with pytest.raises(ValueError, match="belongs to somebody"):
            write_view("", "chart", CHART)


class TestTheDocumentIsOpaque:
    def test_any_shape_the_surface_wants(self, bound):
        """What belongs in a chart's saved state is a question about charts. A
        schema here would make the platform learn every surface's shape."""
        odd = {"a": [1, {"b": None}], "c": {"d": [True, 2.5]}}
        write_view("ben", "anything", odd)
        assert read_view("ben", "anything") == odd

    def test_but_not_an_unbounded_one(self, bound):
        """Past a point a "saved view" is somebody storing their data in the
        state store. Refused by SIZE, because the shape is the consumer's
        business and the size is the platform's."""
        with pytest.raises(ViewTooLarge):
            write_view("ben", "chart", {"blob": "x" * (MAX_DOCUMENT_BYTES + 1)})

    def test_a_document_that_cannot_be_read_opens_fresh(self, bound):
        """Written by a version that is gone, or by hand. A surface that
        cannot read its own saved state should open fresh rather than refuse
        to open: that is the difference between a lost setting and a lost
        page."""
        write_view("ben", "chart", CHART)
        row = bound.get(SavedView, ("ben", "chart", LAST))
        row.document = "{not json"
        bound.commit()
        assert read_view("ben", "chart") is None


class TestItDoesNotBecomeAFilingCabinet:
    def test_a_principal_may_not_hoard_views(self, bound):
        for i in range(MAX_VIEWS_PER_SURFACE):
            write_view("ben", "chart", {"i": i}, name=f"v{i}")
        with pytest.raises(TooManyViews):
            write_view("ben", "chart", {"i": "one too many"}, name="extra")

    def test_but_may_always_overwrite_one_it_has(self, bound):
        for i in range(MAX_VIEWS_PER_SURFACE):
            write_view("ben", "chart", {"i": i}, name=f"v{i}")
        write_view("ben", "chart", {"i": "replaced"}, name="v0")
        assert read_view("ben", "chart", "v0") == {"i": "replaced"}


class TestPinning:
    def test_a_view_is_not_pinned_by_default(self, bound):
        write_view("ben", "chart", CHART, name="startup")
        assert list_views("ben", "chart")[0]["pinned"] is False

    def test_and_pinning_is_remembered(self, bound):
        write_view("ben", "chart", CHART, name="startup", pinned=True)
        assert list_views("ben", "chart")[0]["pinned"] is True

    def test_saving_again_leaves_it_pinned(self, bound):
        """Re-saving the document is not a statement about pinning."""
        write_view("ben", "chart", CHART, name="startup", pinned=True)
        write_view("ben", "chart", {"sites": ["b"]}, name="startup")
        assert list_views("ben", "chart")[0]["pinned"] is True
