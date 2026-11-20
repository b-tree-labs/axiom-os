# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-022..026, spec-video §Events: the ledger is the data platform's DAQ journal, one per source.

An event is appended to ``<state>/video/journal/<source>/`` through a
``DAQConsolidator``, one feed per trigger, so it is stored exactly as a reading is:
gapless ``seq``, hash chain, the trigger named as its model. One writer at a time per
source, across threads and processes. ``events.jsonl`` is read, never written.
Nothing is sent and nothing is compacted.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest

from axiom.extensions.builtins.data_platform.daq import DAQJournal, verify_chain
from axiom.extensions.builtins.video import ledger, runner
from axiom.extensions.builtins.video import triggers as vt
from axiom.extensions.builtins.video.bus import FrameBus

from .harness import PYTHON, child_env, until
from .journaled import feeds, journal_dir, legacy_path, records, segments
from .scenes import crossing

SCRIPTED = "axiom.extensions.builtins.video.tests.trigger_fakes:ScriptedTrigger"


def _event(
    n: int = 1,
    *,
    stream: str = "cam-1",
    trigger: str = "motion@0.1.0",
    kind: str = "occurred",
    ts: str | None = None,
    score: float | None = 0.04,
    severity: str = "info",
    detail: dict | None = None,
) -> vt.TriggerEvent:
    return vt.TriggerEvent(
        stream=stream,
        trigger=trigger,
        ts=ts or f"2026-11-10T12:00:{n % 60:02d}.{n // 60:06d}+00:00",
        kind=kind,
        incident_key=f"{stream}:{trigger}:e{n}",
        severity=severity,
        score=score,
        detail=detail if detail is not None else {"method": "frame_diff", "n": n},
    )


def _append_in_another_process(state_dir, events: list[vt.TriggerEvent]) -> subprocess.Popen:
    """A second process appending to the same state, the way a successor or ``revoke`` does."""
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from axiom.extensions.builtins.video import ledger, triggers\n"
        "for row in json.loads(sys.argv[2]):\n"
        "    ledger.append(triggers.TriggerEvent.from_dict(row), Path(sys.argv[1]))\n"
    )
    return subprocess.Popen(
        [PYTHON, "-c", code, str(state_dir), json.dumps([e.as_dict() for e in events])],
        env=child_env(state_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait(proc: subprocess.Popen) -> None:
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, out + err


def _seqs(recs) -> list[int]:
    return [r.envelope.seq for r in recs]


# ---- VID-022: one journal per source, one feed per trigger, no events.jsonl ----------------


@pytest.mark.acceptance("VID-022")
def test_an_event_lands_in_its_sources_journal_and_nowhere_else(state_dir):
    ledger.append(_event(1), state_dir)
    ledger.append(_event(2, stream="cam-2"), state_dir)

    (one,) = records(state_dir, "cam-1")
    (two,) = records(state_dir, "cam-2")
    assert one.record.tags["incident_key"] == "cam-1:motion@0.1.0:e1"
    assert two.record.tags["incident_key"] == "cam-2:motion@0.1.0:e2"
    assert ledger.journal_dir(state_dir, "cam-1") == journal_dir(state_dir, "cam-1")
    assert not legacy_path(state_dir).exists(), "nothing writes events.jsonl any more"


@pytest.mark.acceptance("VID-022")
def test_each_trigger_is_a_feed_of_its_own_in_one_journal(state_dir):
    ledger.append(_event(1), state_dir)
    ledger.append(_event(2, trigger="scripted@1.0.0"), state_dir)
    ledger.append(_event(3), state_dir)
    assert feeds(state_dir, "cam-1") == {"video.motion", "video.scripted"}
    assert len(segments(state_dir, "cam-1")) == 1, "one journal holds every trigger of a source"


@pytest.mark.acceptance("VID-022")
def test_motion_on_the_bus_lands_in_the_journal(state_dir):
    catalog = vt.TriggerCatalog(
        entries={
            "motion": vt.TriggerEntry(
                "motion", "axiom.extensions.builtins.video.motion:MotionTrigger", "builtin"
            )
        }
    )
    bus = FrameBus("cam-1")
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "motion"}])
    scene = crossing()
    for frame in scene:
        bus.publish(frame)
        time.sleep(0.002)
    until(lambda: (h := triggers.health()[0])["frames"] + h["dropped"] >= len(scene))
    triggers.stop()

    recs = records(state_dir, "cam-1", feed="video.motion")
    assert [r.record.values["kind"] for r in recs] == ["started", "ended"]
    assert len({r.record.tags["incident_key"] for r in recs}) == 1
    assert not legacy_path(state_dir).exists()


# ---- VID-023: a journaled event is a signal record ---------------------------------------


@pytest.mark.acceptance("VID-023")
def test_a_journaled_event_is_a_signal_record(state_dir):
    event = _event(7, kind="started", severity="warning", score=0.125)
    ledger.append(event, state_dir)
    (rec,) = records(state_dir, "cam-1")
    env, r = rec.envelope, rec.record
    assert (env.producer_id, env.feed) == ("video-cam-1", "video.motion")
    assert (env.source_class, env.model_ref) == ("estimated", "motion@0.1.0")
    assert (env.delivery_class, env.payload_kind) == ("standard", "records")
    assert (env.seq, env.prev_hash) == (0, None) and rec.hash_matches()
    assert (r.schema_id, r.ts, r.quality, r.sensitivity) == (
        "video/trigger-event-v1",
        event.ts,
        "good",
        "internal",
    )
    assert dict(r.values) == {"kind": "started", "score": 0.125, "detail": dict(event.detail)}
    assert dict(r.tags) == {
        "stream": "cam-1",
        "trigger": "motion@0.1.0",
        "incident_key": event.incident_key,
        "severity": "warning",
    }


@pytest.mark.acceptance("VID-023")
def test_each_feed_has_a_gapless_seq_and_a_chain_that_verifies(state_dir):
    for n in range(6):
        trigger = "motion@0.1.0" if n % 3 else "scripted@1.0.0"
        ledger.append(_event(n, trigger=trigger), state_dir)
    for feed, count in (("video.motion", 4), ("video.scripted", 2)):
        recs = records(state_dir, "cam-1", feed=feed)
        assert _seqs(recs) == list(range(count))
        assert verify_chain(recs) == []


@pytest.mark.acceptance("VID-023")
def test_a_new_version_of_a_trigger_is_its_new_model_and_the_same_feed(state_dir):
    ledger.append(_event(1, trigger="motion@0.1.0"), state_dir)
    ledger.append(_event(2, trigger="motion@0.2.0"), state_dir)
    recs = records(state_dir, "cam-1", feed="video.motion")
    assert [r.envelope.model_ref for r in recs] == ["motion@0.1.0", "motion@0.2.0"]
    assert _seqs(recs) == [0, 1] and verify_chain(recs) == []


@pytest.mark.acceptance("VID-023")
def test_a_new_holder_continues_the_seq_and_the_chain(state_dir):
    ledger.append(_event(1), state_dir)
    ledger.append(_event(2), state_dir)
    _wait(_append_in_another_process(state_dir, [_event(3), _event(4)]))  # a successor
    ledger.append(_event(5), state_dir)  # and this one again, after it

    recs = records(state_dir, "cam-1", feed="video.motion")
    assert _seqs(recs) == [0, 1, 2, 3, 4]
    assert verify_chain(recs) == []
    assert [r.record.tags["incident_key"][-2:] for r in recs] == ["e1", "e2", "e3", "e4", "e5"]


# ---- VID-024: one writer at a time per source ---------------------------------------------


@pytest.mark.acceptance("VID-024")
def test_triggers_writing_at_once_never_share_a_seq(state_dir):
    def write(trigger: str, base: int) -> None:
        for n in range(base, base + 15):
            ledger.append(_event(n, trigger=trigger), state_dir)

    threads = [
        threading.Thread(target=write, args=(trigger, 100 * i))
        for i, trigger in enumerate(["motion@0.1.0", "motion@0.1.0", "scripted@1.0.0"] * 2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    motion = records(state_dir, "cam-1", feed="video.motion")
    scripted = records(state_dir, "cam-1", feed="video.scripted")
    assert _seqs(motion) == list(range(60)) and verify_chain(motion) == []
    assert _seqs(scripted) == list(range(30)) and verify_chain(scripted) == []


@pytest.mark.acceptance("VID-024")
def test_processes_writing_at_once_never_share_a_seq(state_dir):
    ledger.append(_event(0), state_dir)  # this process has a writer open, and keeps writing
    procs = [
        _append_in_another_process(state_dir, [_event(100 * i + n) for n in range(12)])
        for i in range(1, 4)
    ]
    for n in range(1, 13):
        ledger.append(_event(n), state_dir)
    for proc in procs:
        _wait(proc)
    ledger.append(_event(13), state_dir)

    recs = records(state_dir, "cam-1", feed="video.motion")
    assert _seqs(recs) == list(range(1 + 12 + 36 + 1))
    assert verify_chain(recs) == []


@pytest.mark.acceptance("VID-024")
def test_a_torn_last_line_is_skipped_and_never_stops_the_next_write(state_dir):
    ledger.append(_event(1), state_dir)
    (seg,) = segments(state_dir, "cam-1")
    with seg.open("ab") as fh:
        fh.write(b'{"envelope": {"producer_id": "video-cam-1", "fe')  # power lost mid-write
    assert [r["incident_key"] for r in ledger.read(state_dir)] == ["cam-1:motion@0.1.0:e1"]

    ledger.append(_event(2), state_dir)
    recs = records(state_dir, "cam-1")  # every line parses again
    assert _seqs(recs) == [0, 1] and verify_chain(recs) == []
    assert [r["incident_key"][-2:] for r in ledger.read(state_dir)] == ["e1", "e2"]


@pytest.mark.acceptance("VID-024")
def test_a_line_that_does_not_parse_is_skipped_when_read(state_dir):
    ledger.append(_event(1), state_dir)
    (seg,) = segments(state_dir, "cam-1")
    with seg.open("ab") as fh:
        fh.write(b"not json at all\n")
    assert len(ledger.read(state_dir)) == 1


# ---- VID-025: events reads the journals, exactly as before --------------------------------


@pytest.mark.acceptance("VID-025")
def test_events_reads_every_journal_oldest_first_with_exactly_the_events_fields(state_dir):
    written = [
        _event(1, ts="2026-11-10T12:00:00+00:00", kind="started"),
        _event(2, stream="cam-2", ts="2026-11-10T12:00:03+00:00", detail={}),
        _event(3, ts="2026-11-10T12:00:05+00:00", score=None),
        _event(4, stream="cam-2", ts="2026-11-10T12:00:09+00:00", trigger="scripted@1.0.0"),
    ]
    for event in written:
        ledger.append(event, state_dir)
    assert ledger.read(state_dir) == [e.as_dict() for e in written]
    assert ledger.read(state_dir, stream="cam-2") == [written[1].as_dict(), written[3].as_dict()]
    assert ledger.read(state_dir, since="2026-11-10T12:00:04+00:00") == [
        written[2].as_dict(),
        written[3].as_dict(),
    ]


@pytest.mark.acceptance("VID-025")
def test_within_one_source_events_are_listed_in_the_order_written(state_dir):
    """A closing ``ended`` stamped by the capture service can carry an earlier time than a
    trigger's frame before it; one source's history is never reordered."""
    late, early = (
        _event(1, ts="2026-11-10T12:00:09+00:00"),
        _event(2, ts="2026-11-10T12:00:01+00:00"),
    )
    ledger.append(late, state_dir)
    ledger.append(early, state_dir)
    assert ledger.read(state_dir) == [late.as_dict(), early.as_dict()]


@pytest.mark.acceptance("VID-025")
def test_what_an_earlier_release_wrote_is_still_listed_and_never_written(state_dir):
    from axiom.infra.state import locked_append_jsonl

    old = [
        _event(1, ts="2026-11-09T08:00:00+00:00", kind="started"),
        _event(1, ts="2026-11-09T08:00:07+00:00", kind="ended"),
    ]
    for event in old:
        locked_append_jsonl(legacy_path(state_dir), event.as_dict())
    with legacy_path(state_dir).open("a", encoding="utf-8") as fh:
        fh.write('{"stream": "cam-1", "kin')  # an earlier release lost power too
    before = legacy_path(state_dir).read_bytes()

    new = _event(2, ts="2026-11-10T12:00:00+00:00")
    ledger.append(new, state_dir)
    assert legacy_path(state_dir).read_bytes() == before, "read-only"
    assert ledger.read(state_dir) == [e.as_dict() for e in (*old, new)]
    assert ledger.read(state_dir, stream="cam-2") == []


@pytest.mark.acceptance("VID-025")
def test_no_journal_reads_as_no_events_and_creates_nothing(state_dir):
    assert ledger.read(state_dir) == []
    assert ledger.read(state_dir, stream="cam-1") == []
    assert not (state_dir / "video" / "journal").exists()


# ---- VID-026: nothing sent, nothing compacted -----------------------------------------


@pytest.mark.acceptance("VID-026")
def test_nothing_is_sent_and_nothing_is_compacted(state_dir, monkeypatch):
    monkeypatch.setattr(ledger, "SEGMENT_RECORDS", 2)
    for n in range(5):
        ledger.append(_event(n), state_dir)

    root = journal_dir(state_dir, "cam-1")
    assert len(segments(state_dir, "cam-1")) == 3
    assert not list((root / "cursors").glob("*.json")), "no consumer: no cursor"
    journal = DAQJournal(root)
    assert journal.compact() == 0 and journal.max_bytes is None
    assert len(ledger.read(state_dir)) == 5
