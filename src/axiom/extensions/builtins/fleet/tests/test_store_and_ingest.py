# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Store + ingest: append-only report log, latest projection, node upsert,
and the tenancy rule — site comes from the credential, never the payload.
SQLite via the swappable provider (schedule's pattern)."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from axiom.extensions.builtins.fleet import store
from axiom.extensions.builtins.fleet.db_models import (
    Base,
    FleetLatest,
    FleetNode,
    FleetReport,
)
from axiom.extensions.builtins.fleet.ingest import (
    IngestRefused,
    ingest_reports,
)

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def provider():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    store.set_provider(provider)
    yield factory
    store.reset_provider()
    engine.dispose()


def _reports(kind="heartbeat", payload=None, n=1):
    return [{"kind": kind, "payload": payload or {}} for _ in range(n)]


def test_ingest_creates_node_report_and_latest(session_factory):
    with store.session_scope() as s:
        out = ingest_reports(
            s,
            site="site-a",
            node_id="node-1",
            reporter_principal="@node-1:site-a",
            reports=_reports(),
            now=NOW,
        )
        s.commit()
        assert out.accepted == 1
        node = s.get(FleetNode, "node-1")
        assert node is not None and node.site == "site-a"
        latest = s.get(FleetLatest, ("node-1", "heartbeat"))
        assert latest is not None
        report = s.get(FleetReport, out.report_ids[0])
        assert report.signature_state == "unverified"
        assert report.received_at is not None


def test_latest_projection_tracks_newest_report(session_factory):
    with store.session_scope() as s:
        first = ingest_reports(
            s,
            site="site-a",
            node_id="n",
            reporter_principal="@n:site-a",
            reports=_reports(payload={"seq": 1}),
            now=NOW,
        )
        second = ingest_reports(
            s,
            site="site-a",
            node_id="n",
            reporter_principal="@n:site-a",
            reports=_reports(payload={"seq": 2}),
            now=NOW + timedelta(minutes=5),
        )
        s.commit()
        latest = s.get(FleetLatest, ("n", "heartbeat"))
        assert latest.report_id == second.report_ids[0] != first.report_ids[0]
        # append-only: both report rows remain
        assert s.query(FleetReport).count() == 2


def test_site_comes_from_credential_never_payload(session_factory):
    """A payload claiming another site is stored verbatim as content but
    attribution follows the credential (ingest-sink tenancy rule)."""
    with store.session_scope() as s:
        ingest_reports(
            s,
            site="site-a",
            node_id="n",
            reporter_principal="@n:site-a",
            reports=[{"kind": "heartbeat", "payload": {"site": "site-b"}}],
            now=NOW,
        )
        s.commit()
        assert s.get(FleetNode, "n").site == "site-a"
        assert s.query(FleetReport).one().site == "site-a"


def test_node_cannot_move_site_by_pushing(session_factory):
    """A credential for another site pushing the same node_id is refused —
    otherwise a leaked node name lets one tenant overwrite another's row."""
    with store.session_scope() as s:
        ingest_reports(
            s,
            site="site-a",
            node_id="n",
            reporter_principal="@n:site-a",
            reports=_reports(),
            now=NOW,
        )
        s.commit()
        with pytest.raises(IngestRefused) as exc:
            ingest_reports(
                s,
                site="site-b",
                node_id="n",
                reporter_principal="@n:site-b",
                reports=_reports(),
                now=NOW,
            )
        assert "site" in str(exc.value)


def test_unknown_kind_refused_atomically(session_factory):
    """One bad kind refuses the whole digest — no partial acceptance the
    pusher cannot see."""
    with store.session_scope() as s:
        with pytest.raises(IngestRefused):
            ingest_reports(
                s,
                site="site-a",
                node_id="n",
                reporter_principal="@n:site-a",
                reports=[
                    {"kind": "heartbeat", "payload": {}},
                    {"kind": "weather", "payload": {}},
                ],
                now=NOW,
            )
        s.commit()
        assert s.query(FleetReport).count() == 0


def test_report_count_cap_refused(session_factory):
    with store.session_scope() as s:
        with pytest.raises(IngestRefused):
            ingest_reports(
                s,
                site="site-a",
                node_id="n",
                reporter_principal="@n:site-a",
                reports=_reports(n=999),
                now=NOW,
                max_reports=10,
            )


def test_payload_size_cap_refused(session_factory):
    with store.session_scope() as s:
        with pytest.raises(IngestRefused):
            ingest_reports(
                s,
                site="site-a",
                node_id="n",
                reporter_principal="@n:site-a",
                reports=[{"kind": "heartbeat", "payload": {"blob": "x" * 100}}],
                now=NOW,
                max_payload_chars=50,
            )


def test_cadence_declaration_persists_on_enroll_style_ingest(session_factory):
    with store.session_scope() as s:
        ingest_reports(
            s,
            site="site-a",
            node_id="n",
            reporter_principal="@n:site-a",
            reports=_reports(),
            now=NOW,
            cadences={"heartbeat": 900, "backup": 86400},
        )
        s.commit()
        assert s.get(FleetNode, "n").cadences == {"heartbeat": 900, "backup": 86400}
