# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LS-1 (#1780): the scale fixture, and a ``verify`` that reads its slice only.

- LB-LS1-A4: ``log seed --scale N`` writes N fictional entries across twenty logs at one site,
  gapless and linked per log, with numbered sessions, attachments named by their hash and a few
  sealed periods per log, plus matching Timeline rows; the same call writes the same rows and a
  second call adds nothing; the records door serves the fixture's logs;
- LB-LS1-A10: the load stands the per-row one-copy guard aside (it costs 1 to 5 ms a row, which
  took the nightly past its hour at a million rows) and puts it back, even when a batch fails;
- LB-LS1-A7: it refuses a production or staging node, a bound node, and any database that is not a
  lane's own or a test run's;
- LB-LS1-A8: ``verify`` of a slice reads that slice from the database, not the chain from the
  slice's start to its head, and a really signed slice verifies.
"""

from __future__ import annotations

import hashlib
import json

import pytest

pytest.importorskip("fastapi")

from sqlalchemy import text  # noqa: E402

from axiom.extensions.builtins.attest import scale_fixture as fx  # noqa: E402
from axiom.extensions.builtins.attest import store  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import SITE  # noqa: E402

from .test_j1 import bus, journal_db, live  # noqa: E402,F401

# ruff: noqa: F811  (pytest fixtures imported from the J1 tests)


def _rows(sql: str, **params):
    with store.session_scope() as s:
        return s.execute(text(sql), params).all()


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()  # noqa: S324 - the fixture's own digest recipe


@pytest.mark.acceptance("LB-LS1-A4")
def test_lb_ls1_a4_the_fixture_is_twenty_linked_logs_deterministic_and_idempotent(attest_db):
    with store.session_scope() as s:
        out = fx.seed_records(s, site=SITE, n=400, seed=7)
    assert out.logbooks == fx.log_ids() and len(out.logbooks) == 20
    assert out.per_log == 20 and out.seals_per_log == 3
    per_log = _rows("SELECT logbook, count(*), min(seq), max(seq) FROM attest_records "
                    "WHERE site_id = :s GROUP BY logbook ORDER BY logbook", s=SITE)  # fmt: skip
    assert [tuple(r) for r in per_log] == [(log, 20, 1, 20) for log in fx.log_ids()]
    # every chain links to its own previous row, from the genesis
    chain = _rows("SELECT seq, digest, prev_digest FROM attest_records WHERE site_id = :s "
                  "AND logbook = 'scale_log_03' ORDER BY seq", s=SITE)  # fmt: skip
    assert chain[0].prev_digest == "0" * 64
    assert all(b.prev_digest == a.digest for a, b in zip(chain, chain[1:], strict=False))
    # deterministic: the digest is the documented function of (seed, site, logs, row)
    tag = f"scale:7:{SITE}:scale_log_01:20"
    first = _rows("SELECT digest FROM attest_records WHERE logbook = 'scale_log_01' AND seq = 1")
    assert first[0].digest == _md5(f"{tag}:d1:1") + _md5(f"{tag}:d2:1")
    # idempotent
    with store.session_scope() as s:
        fx.seed_records(s, site=SITE, n=400, seed=7)
    assert _rows("SELECT count(*) FROM attest_records WHERE site_id = :s", s=SITE)[0][0] == 400

    seals = _rows("SELECT logbook, seq, record FROM attest_records WHERE kind = 'seal' "
                  "AND logbook = 'scale_log_01' ORDER BY seq")  # fmt: skip
    assert [r.seq for r in seals] == [5, 10, 15]
    assert seals[0].record["content"]["seal"]["covers"] == {"from_seq": 1, "to_seq": 5, "head_seq": 5}
    att = _rows("SELECT record FROM attest_records WHERE record->'content' ? 'attachments' LIMIT 1")
    (a,) = att[0].record["content"]["attachments"]
    assert len(a["sha256"]) == 64 and a["name"].endswith(".jpg")
    sessions = _rows("SELECT DISTINCT record->'interval'->>'number' AS n FROM attest_records")
    assert {r.n for r in sessions} == {str(fx.SESSION_SEED)}


@pytest.mark.acceptance("LB-LS1-A4")
def test_lb_ls1_a4_the_records_door_serves_the_fixture_logs(attest_db, client):
    fx.register()
    with store.session_scope() as s:
        fx.seed_records(s, site=SITE, n=2000, seed=1)
    c = as_(client, OP1)
    page = c.get("/api/v1/attest/scale_log_05/records?limit=50").json()
    assert page["page"]["newest_seq"] == 100 and len(page["records"]) == 50
    older = c.get("/api/v1/attest/scale_log_05/records?limit=50&before_seq=51").json()
    assert [r["seq"] for r in older["records"]] == list(range(1, 51))
    hits = c.get("/api/v1/attest/scale_log_05/records?limit=50&q=handover%20desk").json()["records"]
    assert hits and all("handover" in r["title"] for r in hits)


@pytest.mark.acceptance("LB-LS1-A4")
def test_lb_ls1_a4_log_seed_writes_entries_and_timeline_rows(live, monkeypatch, capsys, tmp_path):
    from axiom.extensions.builtins.attest import log_cli
    from axiom.extensions.builtins.journal import store as journal_store

    monkeypatch.chdir(tmp_path)
    rc = log_cli.main(["seed", "--scale", "100", "--seed", "3", "--site", SITE, "--declare", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    v = out["value"]
    assert v["rows"] == 100 and v["journal"]["rows"] == 100 and len(v["logbooks"]) == 20
    assert v["database"].startswith("axiom_test_")
    import tomllib

    from axiom.extensions.builtins.attest.logbooks import parse_logbook
    from axiom.extensions.builtins.attest.registry import manifest_entries

    manifest = tmp_path / ".axi" / "extensions" / "scale_fixture" / "axiom-extension.toml"
    declared = [parse_logbook(tomllib.loads(p.read_text()), source=str(p)).id
                for p, _ in manifest_entries(manifest)]  # fmt: skip
    assert declared == fx.log_ids()
    assert _rows("SELECT count(*) FROM attest_records WHERE site_id = :s", s=SITE)[0][0] == 100
    with journal_store.session_scope() as s:
        lanes = dict(s.execute(text("SELECT lane, count(*) FROM journal_events GROUP BY lane")).all())
        logs = s.execute(text("SELECT DISTINCT logbook FROM journal_events WHERE lane = 'attestations'")).scalars().all()
    assert set(lanes) == {"platform", "commands", "agents", "tool_audit", "attestations"}
    assert logs and set(logs) <= set(fx.log_ids())


@pytest.mark.acceptance("LB-LS1-A7")
def test_lb_ls1_a7_the_fixture_refuses_anything_but_a_dev_database(monkeypatch):
    from axiom.extensions.builtins.attest import log_cli
    from axiom.extensions.builtins.attest.skills import scale_seed
    from axiom.infra import serving_mode

    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://localhost/axiom_lane_mine")
    assert fx.refuse_unless_dev() == "axiom_lane_mine"
    for name in ("axiom_db", "axiom", "postgres", "axiom_lane_x;drop"):
        with pytest.raises(fx.ScaleFixtureRefused, match="not a lane's own"):
            fx.refuse_unless_dev(f"postgresql://localhost/{name}")
    for mode in ("production", "staging"):
        monkeypatch.setenv("AXIOM_MODE", mode)
        with pytest.raises(fx.ScaleFixtureRefused, match=f"runs as {mode}"):
            fx.refuse_unless_dev()
    monkeypatch.setenv("AXIOM_MODE", "production")
    refused = scale_seed.run({"scale": 10, "site": SITE})
    assert not refused.ok and "runs as production" in refused.errors[0]
    assert log_cli.main(["seed", "--scale", "10", "--site", SITE]) == 1
    monkeypatch.delenv("AXIOM_MODE")
    monkeypatch.setattr(serving_mode, "resolve_mode", lambda: serving_mode.ServingMode(mode="bound"))
    with pytest.raises(fx.ScaleFixtureRefused, match="bound to a site"):
        fx.refuse_unless_dev()


@pytest.mark.acceptance("LB-LS1-A8")
def test_lb_ls1_a8_verify_reads_only_its_slice(attest_db, node_signer, monkeypatch):
    from axiom.attest import verify as verify_lib
    from axiom.extensions.builtins.attest import service

    keys = {node_signer.key_id: node_signer.public_bytes}
    with store.session_scope() as s:
        head = fx.signed_chain(s, site=SITE, logbook="scale_signed", n=60, signer=node_signer)
    assert head["seq"] == 60
    read = []
    real = service.records

    def spy(*a, **kw):
        rows = real(*a, **kw)
        read.append(len(rows))
        return rows

    monkeypatch.setattr(service, "records", spy)
    report = verify_lib.chain(SITE, "scale_signed", 11, 20, public_keys=keys)
    assert report.ok and report.checked == 10 and report.head_seq == 20
    assert read == [11], "the slice and the one record before it, nothing past to_seq"
    assert verify_lib.chain(SITE, "scale_signed", public_keys=keys).checked == 60


GUARD = "SELECT tgenabled FROM pg_trigger WHERE tgname = 'attest_records_one_copy' AND NOT tgisinternal"


@pytest.mark.acceptance("LB-LS1-A10")
def test_lb_ls1_a10_the_load_stands_the_one_copy_guard_aside_and_puts_it_back(attest_db):
    seen: list[str] = []

    class Spy:
        """A session that records the guard's state each time the fixture writes a batch."""

        def __init__(self, s, fail=False):
            self.s, self.fail = s, fail

        def execute(self, stmt, params=None):
            if str(stmt) == fx.RECORDS_SQL:
                seen.append(self.s.execute(text(GUARD)).scalar())
                if self.fail:
                    raise RuntimeError("a batch failed")
            return self.s.execute(stmt, params) if params is not None else self.s.execute(stmt)

        def __getattr__(self, name):
            return getattr(self.s, name)

    fx.register()
    with store.session_scope() as s:
        fx.seed_records(Spy(s), site=SITE, n=200)
    assert seen and set(seen) == {"D"}  # disabled while rows are written
    assert _rows(GUARD) == [("O",)]  # and enabled again after

    seen.clear()
    with store.session_scope() as s, pytest.raises(RuntimeError):
        fx.seed_records(Spy(s, fail=True), site=SITE, n=200, seed=1)
    assert _rows(GUARD) == [("O",)]  # a failed batch does not leave the guard off
