# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for axiom.infra.trace and axiom.infra.axiom_logging.

Run:
    pytest tests/infra/test_logging.py -v
"""

from __future__ import annotations

import json
import logging
import threading
import time

# ---------------------------------------------------------------------------
# axiom.infra.trace
# ---------------------------------------------------------------------------


class TestTrace:
    """trace_id and session_id propagate through contextvars."""

    def test_new_trace_returns_nonempty_string(self):
        from axiom.infra.trace import new_trace
        tid = new_trace()
        assert isinstance(tid, str)
        assert len(tid) >= 8

    def test_current_trace_matches_new_trace(self):
        from axiom.infra.trace import current_trace, new_trace
        tid = new_trace()
        assert current_trace() == tid

    def test_two_new_trace_calls_produce_different_ids(self):
        from axiom.infra.trace import new_trace
        assert new_trace() != new_trace()

    def test_current_trace_default_when_not_set(self):
        """In a fresh thread, current_trace() returns a non-empty sentinel."""
        from axiom.infra.trace import current_trace
        results = []

        def worker():
            results.append(current_trace())

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert isinstance(results[0], str)
        assert results[0]  # not empty

    def test_trace_is_thread_local(self):
        """Each thread has its own trace_id via contextvars."""
        from axiom.infra.trace import current_trace, new_trace
        ids = {}

        def worker(name):
            ids[name] = new_trace()
            time.sleep(0.01)
            assert current_trace() == ids[name], "trace_id mutated by another thread"

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(ids.values())) == 5, "all threads should have unique trace_ids"

    def test_set_and_get_session(self):
        from axiom.infra.trace import current_session, set_session
        set_session("test-session-abc")
        assert current_session() == "test-session-abc"

    def test_current_session_default_when_not_set(self):
        from axiom.infra.trace import current_session
        results = []

        def worker():
            results.append(current_session())

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert isinstance(results[0], str)
        assert results[0]


# ---------------------------------------------------------------------------
# StructuredJsonFormatter
# ---------------------------------------------------------------------------


class TestStructuredJsonFormatter:
    """Log records are serialised to JSON with required fields."""

    def _make_record(self, msg="test message", level=logging.INFO, name="test.logger"):
        record = logging.LogRecord(
            name=name,
            level=level,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        return record

    def test_output_is_valid_json(self):
        from axiom.infra.axiom_logging import StructuredJsonFormatter
        fmt = StructuredJsonFormatter()
        record = self._make_record()
        output = fmt.format(record)
        data = json.loads(output)
        assert isinstance(data, dict)

    def test_required_fields_present(self):
        from axiom.infra.axiom_logging import StructuredJsonFormatter
        fmt = StructuredJsonFormatter()
        data = json.loads(fmt.format(self._make_record()))
        for field in ("ts", "level", "logger", "trace_id", "session_id", "msg"):
            assert field in data, f"missing required field: {field}"

    def test_level_name_is_string(self):
        from axiom.infra.axiom_logging import StructuredJsonFormatter
        fmt = StructuredJsonFormatter()
        data = json.loads(fmt.format(self._make_record(level=logging.WARNING)))
        assert data["level"] == "WARNING"

    def test_extra_fields_appear_in_output(self):
        from axiom.infra.axiom_logging import StructuredJsonFormatter
        fmt = StructuredJsonFormatter()
        record = self._make_record()
        record.provider = "llm-private"
        record.duration_ms = 42
        data = json.loads(fmt.format(record))
        assert data["provider"] == "llm-private"
        assert data["duration_ms"] == 42

    def test_trace_id_from_context(self):
        from axiom.infra.axiom_logging import StructuredJsonFormatter
        from axiom.infra.trace import new_trace
        fmt = StructuredJsonFormatter()
        tid = new_trace()
        data = json.loads(fmt.format(self._make_record()))
        assert data["trace_id"] == tid

    def test_exc_info_serialised_to_structured_fields(self):
        from axiom.infra.axiom_logging import StructuredJsonFormatter
        fmt = StructuredJsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = self._make_record()
            record.exc_info = sys.exc_info()
            data = json.loads(fmt.format(record))

        assert data["exc_type"] == "ValueError"
        assert "boom" in data["exc_value"]
        assert isinstance(data["exc_traceback"], list)
        assert all("file" in frame for frame in data["exc_traceback"])

    def test_ts_is_iso8601_utc(self):
        from datetime import datetime

        from axiom.infra.axiom_logging import StructuredJsonFormatter
        fmt = StructuredJsonFormatter()
        data = json.loads(fmt.format(self._make_record()))
        # Must parse without error and be UTC
        dt = datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# get_logger
# ---------------------------------------------------------------------------


class TestGetLogger:
    def test_returns_standard_logger(self):
        from axiom.infra.axiom_logging import get_logger
        logger = get_logger("test.mymodule")
        assert isinstance(logger, logging.Logger)

    def test_name_preserved(self):
        from axiom.infra.axiom_logging import get_logger
        logger = get_logger("axiom.ext.my_ext")
        assert logger.name == "axiom.ext.my_ext"

    def test_calling_with_dunder_name_works(self):
        """Simulates get_logger(__name__) from inside a module."""
        from axiom.infra.axiom_logging import get_logger
        logger = get_logger(__name__)
        assert logger is not None

    def test_same_name_returns_same_instance(self):
        from axiom.infra.axiom_logging import get_logger
        a = get_logger("axiom.same.logger")
        b = get_logger("axiom.same.logger")
        assert a is b


# ---------------------------------------------------------------------------
# axiom_signal helper
# ---------------------------------------------------------------------------


class TestNeutSignal:
    def test_returns_dict_with_signal_event_key(self):
        from axiom.infra.axiom_logging import axiom_signal
        result = axiom_signal("connections.vpn_degraded", provider="llm-private", attempts=3)
        assert result["signal_event"] == "connections.vpn_degraded"

    def test_payload_kwargs_nested_under_signal_payload(self):
        from axiom.infra.axiom_logging import axiom_signal
        result = axiom_signal("connections.vpn_degraded", provider="p1", attempts=3)
        assert result["signal_payload"]["provider"] == "p1"
        assert result["signal_payload"]["attempts"] == 3

    def test_empty_kwargs_produces_empty_payload(self):
        from axiom.infra.axiom_logging import axiom_signal
        result = axiom_signal("some.event")
        assert result["signal_payload"] == {}

    def test_result_is_usable_as_extra_dict(self):
        """The dict should be passable directly as logging extra={}."""
        from axiom.infra.axiom_logging import axiom_signal
        extra = axiom_signal("some.event", foo="bar")
        # logging.makeLogRecord will not raise with these keys
        record = logging.LogRecord("t", logging.INFO, "", 0, "msg", (), None)
        record.__dict__.update(extra)
        assert record.signal_event == "some.event"


# ---------------------------------------------------------------------------
# ForensicRingBuffer
# ---------------------------------------------------------------------------


class TestForensicRingBuffer:
    def test_empty_buffer_flush_writes_file(self, tmp_path):
        from axiom.infra.axiom_logging import ForensicRingBuffer
        buf = ForensicRingBuffer(capacity=100)
        out = buf.flush_snapshot(tmp_path / "incident", reason="test")
        assert out.exists()
        lines = out.read_text().splitlines()
        header = json.loads(lines[0])
        assert header["reason"] == "test"
        assert header["record_count"] == 0

    def test_emitted_records_appear_in_snapshot(self, tmp_path):
        from axiom.infra.axiom_logging import ForensicRingBuffer
        buf = ForensicRingBuffer(capacity=100)
        # Emit a record manually
        record = logging.LogRecord("test", logging.DEBUG, "", 0, "hello debug", (), None)
        buf.emit(record)
        out = buf.flush_snapshot(tmp_path / "incident", reason="test")
        lines = out.read_text().splitlines()
        assert len(lines) == 2  # header + 1 record
        entry = json.loads(lines[1])
        assert entry["msg"] == "hello debug"

    def test_capacity_respected(self, tmp_path):
        from axiom.infra.axiom_logging import ForensicRingBuffer
        buf = ForensicRingBuffer(capacity=5)
        for i in range(10):
            rec = logging.LogRecord("t", logging.DEBUG, "", 0, f"msg {i}", (), None)
            buf.emit(rec)
        out = buf.flush_snapshot(tmp_path / "incident", reason="cap test")
        lines = out.read_text().splitlines()
        header = json.loads(lines[0])
        assert header["record_count"] == 5
        # Last 5 messages should be kept
        last = json.loads(lines[-1])
        assert last["msg"] == "msg 9"

    def test_snapshot_filename_includes_timestamp(self, tmp_path):
        from axiom.infra.axiom_logging import ForensicRingBuffer
        buf = ForensicRingBuffer(capacity=10)
        out = buf.flush_snapshot(tmp_path / "incident", reason="ts test")
        # filename pattern: incident.<timestamp>.jsonl
        assert "T" in out.name or out.name.count(".") >= 2

    def test_thread_safe_concurrent_emit(self, tmp_path):
        from axiom.infra.axiom_logging import ForensicRingBuffer
        buf = ForensicRingBuffer(capacity=1000)

        def emitter(n):
            for i in range(50):
                rec = logging.LogRecord("t", logging.DEBUG, "", 0, f"{n}-{i}", (), None)
                buf.emit(rec)

        threads = [threading.Thread(target=emitter, args=(n,)) for n in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        out = buf.flush_snapshot(tmp_path / "incident", reason="concurrency")
        lines = out.read_text().splitlines()
        header = json.loads(lines[0])
        assert header["record_count"] == 500


# ---------------------------------------------------------------------------
# IncidentSnapshotHandler
# ---------------------------------------------------------------------------


class TestIncidentSnapshotHandler:
    def test_error_triggers_snapshot(self, tmp_path):
        from axiom.infra.axiom_logging import ForensicRingBuffer, IncidentSnapshotHandler
        buf = ForensicRingBuffer(capacity=100)
        handler = IncidentSnapshotHandler(ring=buf, snapshot_dir=tmp_path, cooldown_s=0)
        record = logging.LogRecord("test", logging.ERROR, "", 0, "something broke", (), None)
        handler.emit(record)
        snapshots = list(tmp_path.glob("*.jsonl"))
        assert len(snapshots) == 1

    def test_critical_triggers_snapshot(self, tmp_path):
        from axiom.infra.axiom_logging import ForensicRingBuffer, IncidentSnapshotHandler
        buf = ForensicRingBuffer(capacity=100)
        handler = IncidentSnapshotHandler(ring=buf, snapshot_dir=tmp_path, cooldown_s=0)
        record = logging.LogRecord("test", logging.CRITICAL, "", 0, "fatal", (), None)
        handler.emit(record)
        assert len(list(tmp_path.glob("*.jsonl"))) == 1

    def test_warning_does_not_trigger_snapshot(self, tmp_path):
        from axiom.infra.axiom_logging import ForensicRingBuffer, IncidentSnapshotHandler
        buf = ForensicRingBuffer(capacity=100)
        handler = IncidentSnapshotHandler(ring=buf, snapshot_dir=tmp_path, cooldown_s=0)
        record = logging.LogRecord("test", logging.WARNING, "", 0, "hmm", (), None)
        handler.emit(record)
        assert len(list(tmp_path.glob("*.jsonl"))) == 0

    def test_cooldown_prevents_snapshot_storm(self, tmp_path):
        from axiom.infra.axiom_logging import ForensicRingBuffer, IncidentSnapshotHandler
        buf = ForensicRingBuffer(capacity=100)
        handler = IncidentSnapshotHandler(ring=buf, snapshot_dir=tmp_path, cooldown_s=60)
        for _ in range(5):
            rec = logging.LogRecord("test", logging.ERROR, "", 0, "err", (), None)
            handler.emit(rec)
        # Only the first should produce a snapshot
        assert len(list(tmp_path.glob("*.jsonl"))) == 1

    def test_snapshot_contains_pre_error_context(self, tmp_path):
        from axiom.infra.axiom_logging import ForensicRingBuffer, IncidentSnapshotHandler
        buf = ForensicRingBuffer(capacity=100)
        # Emit some context records before the error
        for i in range(3):
            rec = logging.LogRecord("ctx", logging.DEBUG, "", 0, f"context {i}", (), None)
            buf.emit(rec)
        handler = IncidentSnapshotHandler(ring=buf, snapshot_dir=tmp_path, cooldown_s=0)
        err = logging.LogRecord("test", logging.ERROR, "", 0, "the failure", (), None)
        handler.emit(err)
        snapshots = list(tmp_path.glob("*.jsonl"))
        lines = snapshots[0].read_text().splitlines()
        header = json.loads(lines[0])
        assert header["record_count"] == 3  # the 3 context records


# ---------------------------------------------------------------------------
# SignalSink
# ---------------------------------------------------------------------------


class TestSignalSink:
    def test_record_without_signal_event_is_ignored(self):
        from axiom.infra.axiom_logging import SignalSink
        sink = SignalSink(registry={"some.event"})
        emitted = []
        sink._emit = lambda et, payload: emitted.append((et, payload))
        record = logging.LogRecord("t", logging.WARNING, "", 0, "plain warning", (), None)
        sink.write(record.__dict__)
        assert emitted == []

    def test_registered_event_is_promoted(self):
        from axiom.infra.axiom_logging import SignalSink
        sink = SignalSink(registry={"connections.vpn_degraded"})
        emitted = []
        sink._emit = lambda et, payload: emitted.append((et, payload))
        record = logging.LogRecord("t", logging.WARNING, "", 0, "vpn gone", (), None)
        record.__dict__["signal_event"] = "connections.vpn_degraded"
        record.__dict__["signal_payload"] = {"provider": "llm-private"}
        sink.write(record.__dict__)
        assert len(emitted) == 1
        assert emitted[0][0] == "connections.vpn_degraded"
        assert emitted[0][1]["provider"] == "llm-private"

    def test_unregistered_event_is_blocked_and_logged(self, caplog):
        from axiom.infra.axiom_logging import SignalSink
        sink = SignalSink(registry={"known.event"})
        emitted = []
        sink._emit = lambda et, payload: emitted.append((et, payload))
        record = logging.LogRecord("t", logging.WARNING, "", 0, "msg", (), None)
        record.__dict__["signal_event"] = "unknown.unregistered"
        record.__dict__["signal_payload"] = {}
        with caplog.at_level(logging.ERROR):
            sink.write(record.__dict__)
        assert emitted == []
        assert "unregistered" in caplog.text.lower() or "unknown.unregistered" in caplog.text

    def test_ec_records_never_promoted(self):
        from axiom.infra.axiom_logging import SignalSink
        sink = SignalSink(registry={"ec.some_event"}, accepts_ec=False)
        emitted = []
        sink._emit = lambda et, payload: emitted.append((et, payload))
        record = logging.LogRecord("t", logging.ERROR, "", 0, "ec event", (), None)
        record.__dict__["signal_event"] = "ec.some_event"
        record.__dict__["signal_payload"] = {}
        record.__dict__["is_ec_record"] = True
        sink.write(record.__dict__)
        assert emitted == []

    def test_payload_enriched_with_log_metadata(self):
        from axiom.infra.axiom_logging import SignalSink
        sink = SignalSink(registry={"my.event"})
        emitted = []
        sink._emit = lambda et, payload: emitted.append((et, payload))
        record = logging.LogRecord("t", logging.WARNING, "", 0, "msg", (), None)
        record.__dict__["signal_event"] = "my.event"
        record.__dict__["signal_payload"] = {"foo": "bar"}
        sink.write(record.__dict__)
        payload = emitted[0][1]
        assert payload["foo"] == "bar"
        assert "_log_level" in payload
        assert "_log_ts" in payload


# ---------------------------------------------------------------------------
# Signal event registry loading
# ---------------------------------------------------------------------------


class TestSignalEventRegistry:
    def test_load_from_toml(self, tmp_path):
        from axiom.infra.axiom_logging import load_signal_registry
        toml_content = """
[[events]]
event_type = "connections.vpn_degraded"
description = "VPN unreachable"
handler = "TRIAGE"
threshold = "3 failures / 10 min"
justification = "TRIAGE should attempt recovery."

[[events]]
event_type = "ingestion.source_stale"
description = "Source not updated"
handler = "TRIAGE"
threshold = "48h"
justification = "Silent failure."
"""
        registry_path = tmp_path / "signal_event_registry.toml"
        registry_path.write_text(toml_content)
        registry = load_signal_registry(registry_path)
        assert "connections.vpn_degraded" in registry
        assert "ingestion.source_stale" in registry

    def test_empty_file_returns_empty_set(self, tmp_path):
        from axiom.infra.axiom_logging import load_signal_registry
        p = tmp_path / "signal_event_registry.toml"
        p.write_text("")
        assert load_signal_registry(p) == set()

    def test_missing_file_returns_empty_set(self, tmp_path):
        from axiom.infra.axiom_logging import load_signal_registry
        result = load_signal_registry(tmp_path / "nonexistent.toml")
        assert result == set()
