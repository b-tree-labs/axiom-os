# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A key writes only the sources its own site owns.

The face stamped ``site`` from the credential onto every batch, and refused a
batch that asserted another site. It never asked who owned the *source* the
batch was addressed to. On a public edge, where one process holds many sites'
sources, a key for site B that named site A's source landed its rows in site A's
bronze (with B's stamp on them): a cross-site write the metadata check could not
see, because the ``source`` field sits beside the metadata, not in it.

What has to hold:

- a key for one site is refused (403, saying what to do) when it names a source
  another site owns, and nothing is written;
- the refusal for a source that does not exist reads the same as for one that
  belongs to someone else, so a caller cannot list another site's source names
  by trying them;
- a caller with no site (an ungated loopback mount) keeps the loud 422 for an
  unknown source;
- a source name is a name, not a path;
- both lanes obey it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ...agents.plinth.connectors import ConnectorConfig, save_connector  # noqa: E402
from .. import (  # noqa: E402
    make_connector_sink_resolver,
    make_connector_site_resolver,
    make_connector_tabular_sink_resolver,
)
from ..api import build_ingest_router, build_tabular_ingest_router  # noqa: E402
from ..tenancy import IngestGrant  # noqa: E402


class _Policy:
    """Stands in for the tenancy seam: a fixed grant."""

    def __init__(self, site):
        self.site = site

    def grant_for(self, request):
        return IngestGrant(principal=f"@p:{self.site}" if self.site else None,
                           site=self.site, max_access_tier=None)

    def check(self, grant, metadata):
        out = dict(metadata)
        if grant.site:
            out["site"] = grant.site
        return out


@pytest.fixture
def state(tmp_path):
    for name, site in (("site-a-src", "site-a"), ("site-b-src", "site-b")):
        save_connector(
            ConnectorConfig(name=name, kind="push", bronze_root=str(tmp_path / "bronze" / name),
                            site=site, default_disposition="allow", default_tier="restricted"),
            state_dir=tmp_path,
        )
    return tmp_path


def _tabular(state, site):
    app = FastAPI()
    app.include_router(build_tabular_ingest_router(
        sink_resolver=make_connector_tabular_sink_resolver(state_dir=state),
        source_site=make_connector_site_resolver(state_dir=state),
        tenancy=_Policy(site)))
    return TestClient(app)


def _documents(state, site):
    app = FastAPI()
    app.include_router(build_ingest_router(
        sink_resolver=make_connector_sink_resolver(state_dir=state, store=None),
        source_site=make_connector_site_resolver(state_dir=state),
        tenancy=_Policy(site)))
    return TestClient(app)


def _rows(source):
    return {"source": source, "batches": [{"item_id": "i1", "schema_ref": "s/v1",
                                            "rows": [{"ts": "2026-10-20T00:00:00Z", "v": 1}]}]}


def _docs(source):
    return {"source": source, "items": [{"item_id": "d1", "content": "hello"}]}


def _landed(state, source):
    root = state / "bronze" / source
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_a_site_writes_its_own_source(state):
    r = _tabular(state, "site-a").post("/ingest/rows", json=_rows("site-a-src"))
    assert r.status_code == 200, r.text
    assert r.json()["rows_landed"] == 1


def test_a_key_for_one_site_cannot_write_another_sites_source(state):
    r = _tabular(state, "site-b").post("/ingest/rows", json=_rows("site-a-src"))
    assert r.status_code == 403
    assert "site-a-src" in r.text and "site-b" in r.text
    assert "source name your site was given" in r.text
    assert _landed(state, "site-a-src") == []


def test_a_source_that_does_not_exist_is_refused_like_one_that_is_not_yours(state):
    foreign = _tabular(state, "site-b").post("/ingest/rows", json=_rows("site-a-src"))
    absent = _tabular(state, "site-b").post("/ingest/rows", json=_rows("site-z-src"))
    assert foreign.status_code == absent.status_code == 403
    assert foreign.text.replace("site-a-src", "X") == absent.text.replace("site-z-src", "X")


def test_without_a_site_an_unknown_source_is_still_a_loud_422(state):
    r = _tabular(state, None).post("/ingest/rows", json=_rows("site-z-src"))
    assert r.status_code == 422


@pytest.mark.parametrize("name", ["../site-a-src", "a/../site-a-src", "..", "site-a-src/", "x\\y", ".hidden", ""])
def test_a_source_name_is_a_name_not_a_path(state, name):
    r = _tabular(state, "site-a").post("/ingest/rows", json=_rows(name))
    assert r.status_code in (403, 422)
    assert _landed(state, "site-a-src") == []


def test_the_document_lane_obeys_the_same_rule(state):
    assert _documents(state, "site-a").post("/ingest", json=_docs("site-a-src")).status_code == 200
    r = _documents(state, "site-b").post("/ingest", json=_docs("site-a-src"))
    assert r.status_code == 403
    assert _landed(state, "site-a-src") != [] and all(
        "site-b" not in p.read_text(errors="ignore") for p in _landed(state, "site-a-src")
        if p.suffix in {".json", ".jsonl", ".txt"})


def test_an_unsited_source_is_not_claimed_by_this_rule(state):
    save_connector(ConnectorConfig(name="legacy-src", kind="push",
                                   bronze_root=str(state / "bronze" / "legacy-src"),
                                   default_disposition="allow", default_tier="restricted"),
                   state_dir=state)
    assert _tabular(state, "site-a").post("/ingest/rows", json=_rows("legacy-src")).status_code == 200
