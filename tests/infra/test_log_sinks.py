# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the LogSink factory/provider pattern.

Run:
    pytest tests/infra/test_log_sinks.py -v
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record(msg="test", level=logging.INFO, **extra) -> dict:
    """Build a minimal log record dict as StructuredJsonFormatter would produce."""
    r = {
        "ts": "2026-03-19T14:00:00.000Z",
        "level": logging.getLevelName(level),
        "levelno": level,
        "logger": "test.logger",
        "trace_id": "abc123",
        "session_id": "sess001",
        "msg": msg,
        "is_ec_record": False,
    }
    r.update(extra)
    return r


# ---------------------------------------------------------------------------
# LogSinkFactory — registration and creation
# ---------------------------------------------------------------------------

class TestLogSinkFactory:
    def setup_method(self):
        from axiom.infra.log_sinks import LogSinkFactory
        LogSinkFactory.reset()

    def test_register_and_create(self):
        from axiom.infra.log_sinks import LogSinkBase, LogSinkFactory

        class DummySink(LogSinkBase):
            def write(self, record: dict) -> None: ...

        LogSinkFactory.register("dummy", DummySink)
        sink = LogSinkFactory.create("dummy", {"level": "INFO"})
        assert isinstance(sink, DummySink)

    def test_unknown_type_raises_value_error(self):
        from axiom.infra.log_sinks import LogSinkFactory
        with pytest.raises(ValueError, match="[Uu]nknown"):
            LogSinkFactory.create("no_such_sink", {})

    def test_available_lists_registered_types(self):
        from axiom.infra.log_sinks import LogSinkBase, LogSinkFactory

        class Alpha(LogSinkBase):
            def write(self, record: dict) -> None: ...

        class Beta(LogSinkBase):
            def write(self, record: dict) -> None: ...

        LogSinkFactory.register("alpha", Alpha)
        LogSinkFactory.register("beta", Beta)
        assert set(LogSinkFactory.available()) >= {"alpha", "beta"}

    def test_reset_clears_registry(self):
        from axiom.infra.log_sinks import LogSinkBase, LogSinkFactory

        class Temp(LogSinkBase):
            def write(self, record: dict) -> None: ...

        LogSinkFactory.register("temp", Temp)
        LogSinkFactory.reset()
        assert "temp" not in LogSinkFactory.available()

    def test_load_from_toml_creates_enabled_sinks(self, tmp_path):
        from axiom.infra.log_sinks import LogSinkBase, LogSinkFactory

        class FakeSink(LogSinkBase):
            def write(self, record: dict) -> None: ...

        LogSinkFactory.register("fake", FakeSink)

        toml = tmp_path / "logging.toml"
        toml.write_text("""
[[log.sinks]]
type = "fake"
enabled = true
level = "WARNING"

[[log.sinks]]
type = "fake"
enabled = false
level = "INFO"
""")
        sinks = LogSinkFactory.load_from_toml(toml)
        assert len(sinks) == 1   # only the enabled one
        assert isinstance(sinks[0], FakeSink)

    def test_load_from_toml_missing_file_returns_empty(self, tmp_path):
        from axiom.infra.log_sinks import LogSinkFactory
        sinks = LogSinkFactory.load_from_toml(tmp_path / "nonexistent.toml")
        assert sinks == []

    def test_load_from_toml_unknown_type_skipped_with_warning(self, tmp_path, caplog):
        from axiom.infra.log_sinks import LogSinkFactory
        toml = tmp_path / "logging.toml"
        toml.write_text("""
[[log.sinks]]
type = "nonexistent_sink_type"
enabled = true
""")
        with caplog.at_level(logging.WARNING):
            sinks = LogSinkFactory.load_from_toml(toml)
        assert sinks == []
        assert "nonexistent_sink_type" in caplog.text


# ---------------------------------------------------------------------------
# LogSinkBase — level filtering contract
# ---------------------------------------------------------------------------

class TestLogSinkBase:
    def setup_method(self):
        from axiom.infra.log_sinks import LogSinkFactory
        LogSinkFactory.reset()

    def test_record_at_or_above_min_level_accepted(self):
        from axiom.infra.log_sinks import LogSinkBase

        received = []

        class Collector(LogSinkBase):
            def write(self, record: dict) -> None:
                received.append(record)

        sink = Collector({"level": "WARNING"})
        sink.handle(_record(level=logging.WARNING))
        sink.handle(_record(level=logging.ERROR))
        assert len(received) == 2

    def test_record_below_min_level_rejected(self):
        from axiom.infra.log_sinks import LogSinkBase

        received = []

        class Collector(LogSinkBase):
            def write(self, record: dict) -> None:
                received.append(record)

        sink = Collector({"level": "WARNING"})
        sink.handle(_record(level=logging.INFO))
        sink.handle(_record(level=logging.DEBUG))
        assert received == []

    def test_default_level_is_info(self):
        from axiom.infra.log_sinks import LogSinkBase

        received = []

        class Collector(LogSinkBase):
            def write(self, record: dict) -> None:
                received.append(record)

        sink = Collector({})
        sink.handle(_record(level=logging.DEBUG))   # below INFO
        sink.handle(_record(level=logging.INFO))    # at INFO
        assert len(received) == 1

    def test_accepts_ec_false_blocks_ec_records(self):
        from axiom.infra.log_sinks import LogSinkBase

        received = []

        class CloudSink(LogSinkBase):
            accepts_ec = False

            def write(self, record: dict) -> None:
                received.append(record)

        sink = CloudSink({"level": "DEBUG"})
        sink.handle(_record(is_ec_record=True))
        assert received == []

    def test_accepts_ec_false_allows_non_ec_records(self):
        from axiom.infra.log_sinks import LogSinkBase

        received = []

        class CloudSink(LogSinkBase):
            accepts_ec = False

            def write(self, record: dict) -> None:
                received.append(record)

        sink = CloudSink({"level": "DEBUG"})
        sink.handle(_record(is_ec_record=False))
        assert len(received) == 1

    def test_accepts_ec_true_allows_ec_records(self):
        from axiom.infra.log_sinks import LogSinkBase

        received = []

        class SecureSink(LogSinkBase):
            accepts_ec = True

            def write(self, record: dict) -> None:
                received.append(record)

        sink = SecureSink({"level": "DEBUG"})
        sink.handle(_record(is_ec_record=True))
        assert len(received) == 1

    def test_write_errors_are_caught_not_raised(self):
        from axiom.infra.log_sinks import LogSinkBase

        class BrokenSink(LogSinkBase):
            def write(self, record: dict) -> None:
                raise RuntimeError("disk full")

        sink = BrokenSink({"level": "DEBUG"})
        # Must not propagate
        sink.handle(_record())


# ---------------------------------------------------------------------------
# Built-in sink: FileSink
# ---------------------------------------------------------------------------

class TestFileSink:
    def setup_method(self):
        from axiom.infra.log_sinks import LogSinkFactory
        LogSinkFactory.reset()

    def test_writes_jsonl_to_path(self, tmp_path):
        from axiom.infra.log_sinks import FileSink
        path = tmp_path / "out.jsonl"
        sink = FileSink({"path": str(path), "level": "DEBUG"})
        sink.handle(_record("hello"))
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["msg"] == "hello"

    def test_appends_on_successive_writes(self, tmp_path):
        from axiom.infra.log_sinks import FileSink
        path = tmp_path / "out.jsonl"
        sink = FileSink({"path": str(path), "level": "DEBUG"})
        sink.handle(_record("first"))
        sink.handle(_record("second"))
        lines = path.read_text().splitlines()
        assert len(lines) == 2

    def test_uses_locked_append(self, tmp_path):
        """FileSink must use locked_append_jsonl, not bare open()."""
        from axiom.infra.log_sinks import FileSink
        with patch("axiom.infra.log_sinks.locked_append_jsonl") as mock_append:
            sink = FileSink({"path": str(tmp_path / "out.jsonl"), "level": "DEBUG"})
            sink.handle(_record("x"))
            mock_append.assert_called_once()

    def test_missing_path_config_raises_on_init(self):
        from axiom.infra.log_sinks import FileSink
        with pytest.raises((ValueError, KeyError)):
            FileSink({})

    def test_registered_in_factory(self):
        from axiom.infra.log_sinks import LogSinkFactory
        # FileSink registers itself on module import
        assert "file" in LogSinkFactory.available()


# ---------------------------------------------------------------------------
# Built-in sink: NullSink (for standard-mode no-op)
# ---------------------------------------------------------------------------

class TestNullSink:
    def test_accepts_all_records_silently(self):
        from axiom.infra.log_sinks import NullSink
        sink = NullSink({"level": "DEBUG"})
        sink.handle(_record("anything"))  # must not raise

    def test_registered_in_factory(self):
        from axiom.infra.log_sinks import LogSinkFactory
        assert "null" in LogSinkFactory.available()


# ---------------------------------------------------------------------------
# Built-in sink: SignalSink via factory
# ---------------------------------------------------------------------------

class TestSignalSinkFactory:
    def test_registered_in_factory(self):
        from axiom.infra.log_sinks import LogSinkFactory
        assert "signal" in LogSinkFactory.available()

    def test_signal_sink_created_from_config(self, tmp_path):
        from axiom.infra.log_sinks import LogSinkFactory
        # Provide a registry path (empty file is fine for instantiation)
        reg = tmp_path / "signal_event_registry.toml"
        reg.write_text("")
        sink = LogSinkFactory.create("signal", {"registry_path": str(reg)})
        assert sink is not None

    def test_signal_sink_does_not_accept_ec(self, tmp_path):
        from axiom.infra.log_sinks import LogSinkFactory
        reg = tmp_path / "signal_event_registry.toml"
        reg.write_text("")
        sink = LogSinkFactory.create("signal", {"registry_path": str(reg)})
        assert sink.accepts_ec is False


# ---------------------------------------------------------------------------
# LogSinkDispatcher — fan-out to all active sinks
# ---------------------------------------------------------------------------

class TestLogSinkDispatcher:
    def test_dispatches_to_all_sinks(self):
        from axiom.infra.log_sinks import LogSinkBase, LogSinkDispatcher

        counts = [0, 0]

        class S0(LogSinkBase):
            def write(self, record: dict) -> None:
                counts[0] += 1

        class S1(LogSinkBase):
            def write(self, record: dict) -> None:
                counts[1] += 1

        dispatcher = LogSinkDispatcher([S0({"level": "DEBUG"}), S1({"level": "DEBUG"})])
        dispatcher.dispatch(_record())
        assert counts == [1, 1]

    def test_one_sink_failure_does_not_block_others(self):
        from axiom.infra.log_sinks import LogSinkBase, LogSinkDispatcher

        received = []

        class FailingSink(LogSinkBase):
            def write(self, record: dict) -> None:
                raise RuntimeError("broken")

        class GoodSink(LogSinkBase):
            def write(self, record: dict) -> None:
                received.append(record)

        dispatcher = LogSinkDispatcher([
            FailingSink({"level": "DEBUG"}),
            GoodSink({"level": "DEBUG"}),
        ])
        dispatcher.dispatch(_record())
        assert len(received) == 1

    def test_empty_dispatcher_is_fine(self):
        from axiom.infra.log_sinks import LogSinkDispatcher
        dispatcher = LogSinkDispatcher([])
        dispatcher.dispatch(_record())  # must not raise
