# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A long walk checks its signatures ahead in worker processes (#2200), and that changes nothing
about what is caught or how it is reported: one bad signature in a 10,000-entry slice is still
found and named, and when the workers fail in any way the walk checks those signatures itself.
Real Ed25519 keys throughout."""

from __future__ import annotations

import base64
import copy
import sys

import pytest

from axiom.attest import _sigcheck
from axiom.attest.chain import GENESIS, Ed25519Signer, VerifyReport, seal, verify_chain
from axiom.vega.identity.keypair import generate_keypair

N = 10_001


@pytest.fixture(scope="module")
def chain() -> tuple[list[dict], dict[str, bytes]]:
    signer = Ed25519Signer(key_id="node-key-1", keypair=generate_keypair())
    records, prev = [], GENESIS
    for i in range(1, N + 1):
        rec = seal(
            {
                "logbook": "operations_log",
                "meaning": "performed",
                "content": {"title": f"Round {i}"},
            },
            seq=i,
            prev_digest=prev,
            signer=signer,
        )
        records.append(rec)
        prev = rec["digest"]
    return records, {signer.key_id: signer.public_bytes}


def _slice(records: list[dict], first: int, last: int) -> tuple[list[dict], str]:
    """Records ``first..last`` and the anchor before them, as ``attest.verify.chain`` reads them."""
    return list(records[first - 1 : last]), records[first - 2]["digest"]


def _flip_signature(records: list[dict], seq: int) -> None:
    i = next(k for k, r in enumerate(records) if r["seq"] == seq)
    rec = copy.deepcopy(records[i])
    raw = bytearray(base64.b64decode(rec["node_sig"]["sig"]))
    raw[0] ^= 0x01
    rec["node_sig"]["sig"] = base64.b64encode(bytes(raw)).decode("ascii")
    records[i] = rec


@pytest.fixture
def used_workers(monkeypatch) -> list[int]:
    """How many signatures each verify sent to workers."""
    sent: list[int] = []
    real = _sigcheck.submit

    def spy(triples):
        sent.append(sum(t is not None for t in triples))
        return real(triples)

    monkeypatch.setattr(_sigcheck, "submit", spy)
    return sent


def _expected_failure(records: list[dict], bad_seq: int, reason: str) -> VerifyReport:
    first = records[0]["seq"]
    if bad_seq == first:
        return VerifyReport(False, 0, None, None, bad_seq, reason)
    before = next(r for r in records if r["seq"] == bad_seq - 1)
    return VerifyReport(False, bad_seq - first, bad_seq - 1, before["digest"], bad_seq, reason)


def test_a_10000_entry_slice_verifies_with_its_signatures_checked_by_workers(chain, used_workers):
    records, keys = chain
    rows, anchor = _slice(records, 2, N)
    report = verify_chain(rows, keys, start_seq=2, start_prev=anchor)
    assert report == VerifyReport(True, 10_000, N, records[-1]["digest"])
    assert used_workers == [10_000]


@pytest.mark.parametrize("bad_seq", [2, 7_321, N])
def test_one_tampered_signature_in_a_10000_entry_slice_is_caught_and_named(
    chain, used_workers, bad_seq
):
    records, keys = chain
    rows, anchor = _slice(records, 2, N)
    _flip_signature(rows, bad_seq)
    report = verify_chain(rows, keys, start_seq=2, start_prev=anchor)
    assert report == _expected_failure(rows, bad_seq, "bad_signature")
    assert used_workers == [10_000]


def test_the_report_matches_the_inline_walk_for_every_kind_of_break(chain, monkeypatch):
    """Digest, link and signature breaks in one long slice report what the inline walk reports."""
    records, keys = chain
    rows, anchor = _slice(records, 2, 4_001)
    _flip_signature(rows, 3_500)
    tampered = copy.deepcopy(rows[2_000])
    tampered["content"]["title"] = "edited"
    rows[2_000] = tampered
    parallel = verify_chain(rows, keys, start_seq=2, start_prev=anchor)
    monkeypatch.setattr(_sigcheck, "PARALLEL_MIN", 10**9)
    inline = verify_chain(rows, keys, start_seq=2, start_prev=anchor)
    assert parallel == inline == _expected_failure(rows, rows[2_000]["seq"], "digest_mismatch")


def _broken_worker(tmp_path, body: str):
    script = tmp_path / "worker.py"
    script.write_text(body)
    return script


@pytest.mark.parametrize(
    "how",
    ["cannot_start", "exits_with_an_error", "answers_short", "answers_nonsense"],
)
def test_when_the_workers_fail_the_walk_checks_every_signature_itself(
    chain, monkeypatch, tmp_path, how
):
    records, keys = chain
    rows, anchor = _slice(records, 2, 2_501)
    if how == "cannot_start":

        def refuse(*a, **k):
            raise OSError("no processes left")

        monkeypatch.setattr(_sigcheck.subprocess, "Popen", refuse)
    else:
        body = {
            "exits_with_an_error": "import sys; sys.stdin.buffer.read(); sys.exit(1)\n",
            "answers_short": "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'\\x01')\n",
            # every answer "verifies" but in a byte the protocol does not define: never trusted
            "answers_nonsense": (
                "import struct, sys\n"
                "d = sys.stdin.buffer.read(); n = struct.unpack('>I', d[:4])[0]\n"
                "sys.stdout.buffer.write(b'\\x07' * n)\n"
            ),
        }[how]
        monkeypatch.setattr(_sigcheck, "_WORKER", _broken_worker(tmp_path, body))

    assert verify_chain(rows, keys, start_seq=2, start_prev=anchor).ok
    _flip_signature(rows, 1_999)
    report = verify_chain(rows, keys, start_seq=2, start_prev=anchor)
    assert report == _expected_failure(rows, 1_999, "bad_signature")


def test_a_short_walk_starts_no_workers(chain, used_workers):
    records, keys = chain
    rows, anchor = _slice(records, 2, 101)
    assert verify_chain(rows, keys, start_seq=2, start_prev=anchor).ok
    assert used_workers == []


def test_a_generator_is_still_read_lazily_and_checked_inline(chain, used_workers):
    records, keys = chain
    rows, anchor = _slice(records, 2, 3_001)
    report = verify_chain(iter(rows), keys, start_seq=2, start_prev=anchor)
    assert report.ok and report.checked == 3_000
    assert used_workers == []


def test_the_worker_imports_nothing_from_the_platform():
    src = _sigcheck._WORKER.read_text()
    assert "axiom" not in src.split('"""', 2)[2]
    assert sys.executable  # the workers run under the caller's own interpreter
