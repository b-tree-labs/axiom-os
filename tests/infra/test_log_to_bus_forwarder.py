# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A log record marked for the bus reaches the bus (#2137, ADR-265 step 1).

Found by the ADR-265 renames: the sink imported ``axiom.infra.events``, which does not exist, and
swallowed the error, so no marked record ever reached the bus. A second gap hid behind it: the
registry that gates promotion was read from a gitignored ``runtime/`` path and shipped nowhere, so
with it empty every event was blocked at the gate. ADR-265 also retires the word "signal": the
sink is a log-to-bus forwarder, and the old names stay readable for one release (expand first).

- BF-1: a registered marked record is published on the default bus: its subject, its payload and
  the log level and time, with the source ``log``.
- BF-2: an unregistered event, and an EC record, never reach the bus.
- BF-3: a subscriber that itself logs a marked record does not loop.
- BF-4: a bus that fails is reported once, not silently, and the log call is unaffected.
- BF-5: the registry the package ships holds every event the code emits, and the default sink
  reads it; a site's own file adds to it.
- BF-6: the old names still work for one release (``SignalSink``, ``axiom_signal``,
  ``load_signal_registry``, the ``signal_event`` and ``signal_payload`` keys) and the new names
  are the same objects or the same keys.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from axiom.infra import axiom_logging as AL
from axiom.infra.bus import EventBus, set_default_eventbus


@pytest.fixture()
def bus():
    fresh = EventBus()
    set_default_eventbus(fresh)
    yield fresh
    set_default_eventbus(None)


def _collect(bus, pattern="*.*"):
    """What a subscriber is handed: ``(subject, payload)``."""
    got = []
    bus.subscribe(pattern, lambda subject, payload: got.append((subject, payload)))
    return got


def _record(event="x.y", **extra):
    return {"signal_event": event, "signal_payload": {"a": 1}, "levelname": "WARNING", "created": 12.5, **extra}


# -- BF-1 ------------------------------------------------------------------------------


def test_bf1_a_registered_marked_record_is_published_on_the_default_bus(bus):
    got = _collect(bus)
    AL.BusForwardSink(registry={"x.y"}).write(_record())
    assert got == [("x.y", {"a": 1, "_log_level": "WARNING", "_log_ts": 12.5})]
    assert [e.source for e in bus.history] == ["log"]


def test_bf1_the_new_record_keys_are_read_too(bus):
    got = _collect(bus)
    AL.BusForwardSink(registry={"x.y"}).write(
        {"bus_event": "x.y", "bus_payload": {"b": 2}, "levelname": "INFO", "created": 1.0}
    )
    assert [payload["b"] for _, payload in got] == [2]


def test_bf1_a_record_with_no_marker_costs_nothing(bus):
    got = _collect(bus)
    AL.BusForwardSink(registry={"x.y"}).write({"levelname": "INFO", "created": 1.0})
    assert got == []


# -- BF-2 ------------------------------------------------------------------------------


def test_bf2_an_unregistered_event_is_blocked_and_an_ec_record_never_goes(bus):
    got = _collect(bus)
    sink = AL.BusForwardSink(registry={"x.y"})
    sink.write(_record("not.registered"))
    sink.write(_record(is_ec_record=True))
    assert got == []


# -- BF-3 ------------------------------------------------------------------------------


def test_bf3_a_subscriber_that_logs_a_marked_record_does_not_loop(bus):
    sink = AL.BusForwardSink(registry={"x.y"})
    seen = []

    def handler(subject, payload):
        seen.append(subject)
        sink.write(_record())  # the subscriber's own log call goes to the same sink

    bus.subscribe("x.y", handler)
    sink.write(_record())
    assert seen == ["x.y"]


# -- BF-4 ------------------------------------------------------------------------------


def test_bf4_a_failing_bus_is_reported_once_and_the_log_call_is_unaffected(monkeypatch, caplog):
    import axiom.infra.bus as bus_module

    def broken():
        raise RuntimeError("no bus")

    monkeypatch.setattr(bus_module, "get_default_eventbus", broken)
    sink = AL.BusForwardSink(registry={"x.y"})
    with caplog.at_level(logging.WARNING):
        sink.write(_record())
        sink.write(_record())
    said = [r for r in caplog.records if "bus" in r.getMessage().lower() and "x.y" in r.getMessage()]
    assert len(said) == 1


# -- BF-5 ------------------------------------------------------------------------------

_EMITTED = (
    "security.chunk_injection_detected",
    "security.response_scan_hit",
    "security.ec_leakage_suspected",
    "logs.size_warning",
)


def test_bf5_the_shipped_registry_holds_every_event_the_code_emits():
    assert set(_EMITTED) <= AL.shipped_bus_event_registry()


def test_bf5_every_event_a_call_site_marks_is_in_the_shipped_registry():
    src = Path(__file__).resolve().parents[2] / "src" / "axiom"
    names = set()
    for path in src.rglob("*.py"):
        if path.name == "axiom_logging.py":
            continue  # the helpers' own docstrings carry an example, not a call site
        text = path.read_text(encoding="utf-8")
        if "axiom_signal(" not in text and "axiom_bus_event(" not in text:
            continue
        names |= set(re.findall(r"axiom_(?:signal|bus_event)\(\s*\"([a-z_]+\.[a-z_.]+)\"", text))
    assert names, "the scan found no call sites; the pattern is wrong"
    assert names <= AL.shipped_bus_event_registry(), sorted(names - AL.shipped_bus_event_registry())


def test_bf5_a_sites_own_file_adds_to_the_shipped_events(tmp_path):
    own = tmp_path / "bus_event_registry.toml"
    own.write_text('[[events]]\nevent_type = "site.custom"\n', encoding="utf-8")
    merged = AL.load_bus_event_registry(own)
    assert "site.custom" in merged
    assert set(_EMITTED) <= merged


def test_bf5_the_legacy_file_name_is_still_read(tmp_path):
    legacy = tmp_path / "signal_event_registry.toml"
    legacy.write_text('[[events]]\nevent_type = "site.legacy"\n', encoding="utf-8")
    assert "site.legacy" in AL.load_bus_event_registry(tmp_path / "bus_event_registry.toml")


def test_bf5_the_default_sink_promotes_a_shipped_event(bus):
    got = _collect(bus, "security.*")
    AL._get_or_create_bus_sink().write(_record("security.response_scan_hit"))
    assert [subject for subject, _ in got] == ["security.response_scan_hit"]


# -- BF-6 ------------------------------------------------------------------------------


def test_bf6_the_old_names_are_the_same_objects_and_keys():
    assert AL.SignalSink is AL.BusForwardSink
    marked = AL.axiom_bus_event("x.y", n=1)
    assert marked == {"bus_event": "x.y", "bus_payload": {"n": 1}}
    legacy = AL.axiom_signal("x.y", n=1)
    assert legacy == {"signal_event": "x.y", "signal_payload": {"n": 1}}


def test_bf6_the_old_loader_still_reads_only_the_file_it_is_given(tmp_path):
    # Kept for one release as it was: callers that want the shipped events use load_bus_event_registry.
    path = tmp_path / "signal_event_registry.toml"
    path.write_text("")
    assert AL.load_signal_registry(path) == set()
    assert AL.load_signal_registry(tmp_path / "missing.toml") == set()
    path.write_text('[[events]]\nevent_type = "a.b"\n', encoding="utf-8")
    assert AL.load_signal_registry(path) == {"a.b"}


# -- BF-7: through the real logger --------------------------------------------------------


@pytest.mark.parametrize("mark", [AL.axiom_bus_event, AL.axiom_signal])
def test_bf7_a_marked_log_call_reaches_the_bus_under_either_key(bus, mark):
    got = _collect(bus, "security.*")
    AL.get_logger("bf7.check").warning("scan hit", extra=mark("security.response_scan_hit", corpus="c"))
    assert [(subject, payload["corpus"]) for subject, payload in got] == [("security.response_scan_hit", "c")]


def test_bf7_an_unregistered_or_ec_log_call_never_reaches_the_bus(bus):
    got = _collect(bus, "security.*")
    log = AL.get_logger("bf7.check")
    log.warning("made up", extra=AL.axiom_bus_event("security.made_up"))
    log.warning("ec", extra={**AL.axiom_bus_event("security.response_scan_hit"), "is_ec_record": True})
    assert got == []
