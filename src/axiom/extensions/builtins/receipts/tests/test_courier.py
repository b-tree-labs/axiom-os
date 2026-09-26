# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The oversight courier: deterministic brief, R14 budget + deltas,
the propose floor, receipts verbatim."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.fleet import store as fleet_store
from axiom.extensions.builtins.fleet.db_models import Base as FleetBase
from axiom.extensions.builtins.fleet.ingest import ingest_reports
from axiom.extensions.builtins.receipts import store as rcpt_store
from axiom.extensions.builtins.receipts.brief import (
    NEEDS_YOU_CAP,
    compose_brief,
    fleet_source,
    render_brief_text,
)
from axiom.extensions.builtins.receipts.db_models import Base as RcptBase
from axiom.extensions.builtins.receipts.skills.direct import run as direct

# Reports are ingested at REAL wall-clock time; evaluating at a fixed
# future instant would render everything STALE (this bit once). Fix the
# instant per test run, but keep it a constant within each test so the
# byte-stability assertion is meaningful.
NOW = datetime.now(UTC)
CADENCES = {"heartbeat": 900, "service_health": 900, "backup": 86_400}


def _mem_provider(base):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def provider():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return provider, engine


@pytest.fixture()
def stores():
    fprov, fe = _mem_provider(FleetBase)
    rprov, re_ = _mem_provider(RcptBase)
    fleet_store.set_provider(fprov)
    rcpt_store.set_provider(rprov)
    yield
    fleet_store.reset_provider()
    rcpt_store.reset_provider()
    fe.dispose()
    re_.dispose()


def _seed(node="node-a", *, healthy=True, at=NOW):
    with fleet_store.session_scope() as s:
        ingest_reports(
            s,
            site="site-a",
            node_id=node,
            reporter_principal=f"@{node}:site-a",
            cadences=CADENCES,
            reports=[
                {"kind": "heartbeat", "payload": {}},
                {
                    "kind": "service_health",
                    "payload": {
                        "services": [
                            {"name": "api", "status": "healthy", "latency_ms": 12}
                            if healthy
                            else {"name": "api", "status": "unhealthy"}
                        ]
                    },
                },
            ],
        )
        s.commit()


def _items():
    with fleet_store.session_scope() as fs:
        return fleet_source(fs, now=NOW)


def test_brief_is_byte_stable(stores):
    _seed()
    items = _items()
    with rcpt_store.session_scope() as rs:
        a = render_brief_text(compose_brief(rs, items, now=NOW, snapshot=False))
        b = render_brief_text(compose_brief(rs, items, now=NOW, snapshot=False))
    assert a == b  # the courier doctrine: deterministic to the words


def test_needs_you_is_budgeted(stores):
    for i in range(6):
        _seed(node=f"node-{i}", healthy=False)
    items = _items()
    with rcpt_store.session_scope() as rs:
        brief = compose_brief(rs, items, now=NOW, snapshot=False)
    assert len(brief.needs_you) == NEEDS_YOU_CAP
    assert brief.counts["needs_you_overflow"] > 0
    text = render_brief_text(brief)
    assert "more in drill-down" in text


def test_trust_deltas_come_from_the_recorded_baseline(stores):
    _seed(healthy=False)  # unproven-ish service_health? unhealthy -> failed
    items = _items()
    with rcpt_store.session_scope() as rs:
        first = compose_brief(rs, items, now=NOW, snapshot=True)
        rs.commit()
    assert first.trust_deltas == []  # no baseline yet -> nothing invented

    _seed(healthy=True)  # verdict changes
    items2 = _items()
    with rcpt_store.session_scope() as rs:
        second = compose_brief(rs, items2, now=NOW + timedelta(minutes=5), snapshot=True)
        rs.commit()
    changed = [d for d in second.trust_deltas if d["claim_kind"] == "service_health"]
    assert changed and changed[0]["from"] != changed[0]["to"]


def test_quiet_line_when_nothing_changed(stores):
    _seed(healthy=True)
    items = _items()
    with rcpt_store.session_scope() as rs:
        compose_brief(rs, items, now=NOW, snapshot=True)
        rs.commit()
    with rcpt_store.session_scope() as rs:
        brief = compose_brief(rs, items, now=NOW + timedelta(minutes=5), snapshot=False)
    # heartbeat + service_health green; unchanged -> the one-line compress
    assert "nothing you rely on changed" in brief.quiet_line


def test_unwired_sources_are_named_not_implied(stores):
    _seed()
    with rcpt_store.session_scope() as rs:
        brief = compose_brief(rs, _items(), now=NOW, snapshot=False)
    assert "session" in brief.not_yet_wired  # honesty about plane coverage


def test_mcp_direct_proposes_and_never_displaces_active(stores):
    r = direct({"site": "", "text": "ship the courier", "mode": "set", "set_by": "@ben:cli"})
    assert r.ok
    r2 = direct(
        {"site": "", "text": "rewrite everything", "mode": "propose", "set_by": "@agent:mcp"}
    )
    assert not r2.ok and "cannot displace" in r2.errors[0]
    r3 = direct({"site": "", "mode": "get"})
    assert r3.value["focus"]["text"] == "ship the courier"
    assert r3.value["focus"]["set_by"] == "@ben:cli"


def test_proposed_focus_renders_distinctly(stores):
    direct({"site": "", "text": "try the new seam", "mode": "propose", "set_by": "@agent:mcp"})
    _seed()
    with rcpt_store.session_scope() as rs:
        text = render_brief_text(compose_brief(rs, _items(), now=NOW, snapshot=False))
    assert "PROPOSED, unconfirmed" in text
    assert "@agent:mcp" in text


# --- The terminology ledger's can-fail check ---------------------------


def _internal_only_terms() -> list[str]:
    """Parse INTERNAL-ONLY terms from the ledger — the ledger IS the
    allowlist, so adding a term there is the review."""
    ledger = Path(__file__).parents[5].parent / "docs" / "conventions" / "terminology-ledger.md"
    text = ledger.read_text(encoding="utf-8")
    section = text.split("**INTERNAL-ONLY**", 1)[1].split("##", 1)[0]
    # terms are separated by middle dots; drop parentheticals and colons
    head = section.split(":", 1)[1]
    terms = []
    for raw in head.replace("\n", " ").split("·"):
        term = raw.split("(")[0].strip().strip(".").lower()
        if term and len(term) > 2:
            terms.append(term)
    return terms


def test_rendered_surfaces_carry_no_internal_only_terms(stores):
    """Operator-facing output must speak plain (or introduced-once)
    language. Composes a brief spanning failed/stale/unproven and
    checks the RENDERED text, plus the webui copy maps, against the
    ledger's internal-only list. Control: 'seat' is on the list and
    genuinely absent — and injecting it makes this fail."""
    terms = _internal_only_terms()
    assert "seat" in terms  # the parser found the list at all

    _seed(node="node-a", healthy=False)
    _seed(node="node-b", healthy=True)
    with rcpt_store.session_scope() as rs:
        text = render_brief_text(compose_brief(rs, _items(), now=NOW, snapshot=False)).lower()
    offenders = [t for t in terms if t in text]
    assert not offenders, f"brief renders internal-only terms: {offenders}"

    # actions.tsx used to be scanned here too — a second, hand-maintained
    # copy of the next-action wording. It is gone: the server composes
    # that once now (receipts.conditions) and the case renders it.
    webui = Path(__file__).parents[1] / "webui" / "src"
    for name in ("microcopy.ts",):
        copy = (webui / name).read_text(encoding="utf-8").lower()
        # scan only string contents loosely: the whole file minus imports
        offenders = [t for t in terms if t in copy and t not in ("witness",)]
        assert not offenders, f"{name} carries internal-only terms: {offenders}"

    # ...and the table that replaced it is held to the same rule.
    from axiom.extensions.builtins.receipts.conditions import CONDITIONS

    for key, condition in CONDITIONS.items():
        said = f"{condition.headline} {condition.detail} {condition.fix}".lower()
        offenders = [t for t in terms if t in said]
        assert not offenders, f"condition {key} carries internal-only terms: {offenders}"


def test_failed_evidence_is_labeled_a_counterexample(stores):
    _seed(node="node-a", healthy=False)  # unhealthy service -> FAILED
    with rcpt_store.session_scope() as rs:
        text = render_brief_text(compose_brief(rs, _items(), now=NOW, snapshot=False))
    assert "counterexample:" in text
