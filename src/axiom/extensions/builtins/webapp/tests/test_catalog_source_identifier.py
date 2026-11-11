# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The catalog queries take a relation name. It has to be an identifier."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.webapp.catalog import series, sync


class _Session:
    def execute(self, *_args, **_kwargs):
        raise AssertionError("a rejected source must not be queried")

    def get_bind(self):
        raise AssertionError("a rejected source must not be queried")


@pytest.mark.parametrize(
    "source",
    [
        "gold.signals; drop table gold.signals",
        "gold.signals, public.users",
        "",
        "gold..signals",
        'gold."signals"',
    ],
)
def test_bucketed_series_refuses_a_source_that_is_not_an_identifier(source):
    with pytest.raises(ValueError, match="identifier"):
        series.bucketed_series(
            _Session(),
            site="s",
            feed="f",
            channels=["c"],
            bucket_s=60,
            source=source,
        )


@pytest.mark.parametrize(
    "source",
    ["signals", "gold.signals"],
)
def test_bucketed_series_accepts_a_plain_relation_name(source):
    """Validation stops at the name. A later failure is the query, not the check."""

    class _Bind:
        class dialect:
            name = "sqlite"

    class _RowSession(_Session):
        def get_bind(self):
            return _Bind()

        def execute(self, *_args, **_kwargs):
            return self

        def fetchall(self):
            return []

    out = series.bucketed_series(
        _RowSession(),
        site="s",
        feed="f",
        channels=["c"],
        bucket_s=60,
        source=source,
    )
    assert out == {"series": []}


def test_project_catalog_refuses_a_source_that_is_not_an_identifier():
    with pytest.raises(ValueError, match="identifier"):
        sync.project_catalog(_Session(), source="gold.signals; drop table t")
