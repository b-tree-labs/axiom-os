# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S10 Journal, Timeline feed and Tail (#1818): every record the run signed is one reference in the
attestations lane, a replay adds nothing, the journal verifies, and a Tail of the lane receives
the run's later entries in order with one read recorded for the subscription."""

from __future__ import annotations

import pytest

from .conftest import ANN, check, open_session, rows_of, sign_cli, stream_frames


def _run(world):
    open_session(world)
    signed = [check(world), sign_cli(ANN, "NOTE", {"text": "a note"})]
    return signed


@pytest.mark.acceptance("LB-E2E-A21")
def test_lb_e2e_a21_every_record_is_one_reference_and_a_replay_adds_nothing(world, live, bus):
    from axiom.extensions.builtins.attest import service
    from axiom.extensions.builtins.journal import rights
    from axiom.extensions.builtins.journal import service as journal

    _run(world)
    signed_ids = [r["attestation_id"] for r in service.records("example-site", "e2e_log")]
    caller = rights.Caller(handle=ANN.handle, sites=None)

    def refs():
        rows = journal.query(caller, lanes=["attestations"], limit=500)["rows"]
        return [r["ref"]["attestation_id"] for r in rows if r["subject"].endswith(".signed")]

    assert sorted(refs()) == sorted(signed_ids) and len(refs()) == len(set(refs()))
    for r in journal.query(caller, lanes=["attestations"], limit=500)["rows"]:
        assert "fields" not in r["ref"] and "content" not in r["ref"]
    bus.replay()
    assert live.catch_up()["attest_records"]["appended"] == 0
    assert sorted(refs()) == sorted(signed_ids)
    assert journal.verify(state_dir=world.state_dir)["ok"]


@pytest.mark.acceptance("LB-E2E-A22")
def test_lb_e2e_a22_a_tail_of_the_lane_gets_the_later_entries_in_order_and_one_read(
    world, journal_app
):
    from axiom.extensions.builtins.journal import rights
    from axiom.extensions.builtins.journal import service as journal

    from .conftest import access_records

    _run(world)
    before = journal.query(
        rights.Caller(handle=ANN.handle, sites=None), lanes=["attestations"], limit=1
    )
    after_seq = before["rows"][0]["seq"]
    later = [sign_cli(ANN, "NOTE", {"text": f"later {i}"}) for i in range(3)]
    status, frames = stream_frames(journal_app, ANN,
                                   f"/api/v1/journal/stream?lanes=attestations&last_event_id={after_seq}&once=1",
                                   until=lambda fs: False, seconds=3.0)  # fmt: skip
    assert status == 200
    got = [r["ref"]["attestation_id"] for r in rows_of(frames) if r["subject"].endswith(".signed")]
    assert got == [r["attestation_id"] for r in later]
    reads = [r for r in access_records(world.state_dir) if r.get("skill") == "journal.read.stream"]
    assert len(reads) == 1 and reads[0]["access"]["after"] == after_seq
