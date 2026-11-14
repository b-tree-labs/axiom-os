# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S13 Integrity (#1818): a row changed behind the triggers is named as the first break by every
checker; a clean run ends with the whole chain, each session's slice, the standalone verifier
and the journal all agreeing, and the declaration unchanged."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys

import pytest

from .conftest import ANN, BO, E2E_LOG, LOG, SITE, check, keys, open_session, sign_cli


def _run(world):
    open_session(world)
    a = check(world)
    b = sign_cli(ANN, "NOTE", {"text": "second"})
    sign_cli(ANN, "CLOSE", {"text": "done"})
    open_session(world)
    sign_cli(ANN, "NOTE", {"text": "next session"})
    return a, b


def _tamper(seq: int) -> None:
    from sqlalchemy import text

    from axiom.extensions.builtins.attest import store

    with store.session_scope() as s:
        s.execute(text("ALTER TABLE attest_records DISABLE TRIGGER attest_records_append_only"))
        s.execute(text("UPDATE attest_records SET record = jsonb_set(record, '{meaning}', '\"approved\"') "
                       "WHERE logbook = :l AND seq = :seq"), {"l": LOG, "seq": seq})  # fmt: skip
        s.execute(text("ALTER TABLE attest_records ENABLE TRIGGER attest_records_append_only"))
        s.commit()


@pytest.mark.acceptance("LB-E2E-A26")
def test_lb_e2e_a26_a_changed_row_is_named_as_the_first_break_everywhere(world):
    from axiom.extensions.builtins.attest import integrity, service, signing
    from axiom.extensions.builtins.hygiene import node_health

    _a, b = _run(world)
    anchor = service.anchor_site(SITE, signing.signer())
    _tamper(b["seq"])
    report = service.verify_logbook(SITE, LOG, keys())
    assert not report.ok and report.first_bad_seq == b["seq"] and report.reason == "digest_mismatch"
    findings = node_health.check_attestation_chains(
        facts=integrity.check_incremental(world.state_dir)
    )
    assert [f.current_value for f in findings] == ["digest_mismatch"]
    assert f"sequence {b['seq']}" in findings[0].message
    # A plain edit changes the content, not the stored digests, so the anchor's fixed heads still
    # match and the chain check is what catches it; a rewrite re-signed to fool the chain is the
    # anchor's job, proved in LB-X2-A5. Both are needed, and here the chain names the break.
    ok, _reason = service.verify_site_anchor(anchor["anchor_id"], keys())
    assert ok


@pytest.mark.acceptance("LB-E2E-A26")
def test_lb_e2e_a26_the_logs_integrity_contacts_are_notified(world):
    # #1812: ``integrity_notify`` names the roles told of a break: one notice per break, per
    # person, however many heartbeats see it.
    from axiom.extensions.builtins.attest import integrity

    told: list[tuple[str, dict]] = []
    integrity.set_sender(lambda who, fact: told.append((who, fact)))
    try:
        _a, b = _run(world)
        _tamper(b["seq"])
        integrity.check_incremental(world.state_dir)
        integrity.check_incremental(world.state_dir)
    finally:
        integrity.reset_sender()
    assert [who for who, _ in told] == [BO.handle], "the lead, once"
    fact = told[0][1]
    assert (fact["logbook"], fact["first_bad_seq"], fact["reason"]) == (
        LOG, b["seq"], "digest_mismatch"
    )


@pytest.mark.acceptance("LB-E2E-A27")
def test_lb_e2e_a27_the_run_ends_verified_everywhere_with_the_declaration_unchanged(
    world, live, tmp_path
):
    from axiom.attest import verify as verify_lib
    from axiom.attest.tools import verify as standalone
    from axiom.extensions.builtins.attest import service
    from axiom.extensions.builtins.journal import service as journal

    declared = hashlib.sha256(E2E_LOG.read_bytes()).hexdigest()
    _run(world)
    whole = service.verify_logbook(SITE, LOG, keys())
    assert whole.ok and whole.checked == len(service.records(SITE, LOG))
    records = service.records(SITE, LOG)
    sessions: dict[int, list[int]] = {}
    for r in records:
        if r.get("interval"):
            sessions.setdefault(r["interval"]["number"], []).append(r["seq"])
    assert len(sessions) == 2
    for seqs in sessions.values():
        part = verify_lib.chain(SITE, LOG, min(seqs), max(seqs), public_keys=keys())
        assert part.ok and part.checked == max(seqs) - min(seqs) + 1
    (tmp_path / "chain.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    (tmp_path / "keys.json").write_text(json.dumps({k: base64.b64encode(v).decode() for k, v in keys().items()}),
                                        encoding="utf-8")  # fmt: skip
    (tmp_path / "verify.py").write_bytes(open(standalone.__file__, "rb").read())
    run = subprocess.run([sys.executable, "-I", "verify.py", "chain.jsonl", "--keys", "keys.json"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=120, env={})  # fmt: skip
    assert run.returncode == 0 and whole.head_digest in run.stdout, run.stdout + run.stderr
    assert journal.verify(state_dir=world.state_dir)["ok"]
    assert hashlib.sha256(E2E_LOG.read_bytes()).hexdigest() == declared
