# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ingest singleton follows a caller that names a bus.

Found by the pre-push hook: a webapp test built the API router, whose journal contribution
started ingest on that process's default bus with a ticker; a later test then asked
``start(bus=its_own)`` and got the already-started singleton back, subscribed to the wrong bus,
so its live writes went nowhere and another extension's events were swallowed into the wrong
schema. A start on a different bus or state dir now restarts; the same bus is a no-op.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.journal import ingest
from axiom.infra.bus import EventBus


@pytest.fixture
def stopped():
    ingest.stop()
    yield
    ingest.stop()


def test_a_start_on_a_different_bus_moves_the_subscription(stopped, tmp_path):
    a, b = EventBus(), EventBus()
    ing = ingest.start(bus=a, state_dir=tmp_path, ticker=False, catch_up=False)
    assert ing.bus is a and ing.started
    assert a.subscribers_for("made_up.thing") and not b.subscribers_for("made_up.thing")
    again = ingest.start(bus=b, state_dir=tmp_path, ticker=False, catch_up=False)
    assert again is ing and ing.bus is b
    assert not a.subscribers_for("made_up.thing") and b.subscribers_for("made_up.thing")


def test_a_start_on_the_same_bus_is_a_no_op(stopped, tmp_path):
    a = EventBus()
    ing = ingest.start(bus=a, state_dir=tmp_path, ticker=False, catch_up=False)
    first = ing._sub
    assert ingest.start(bus=a, state_dir=tmp_path, ticker=False, catch_up=False)._sub is first
    assert len(a.subscribers_for("made_up.thing")) == 1


def test_a_different_state_dir_restarts_too(stopped, tmp_path):
    a = EventBus()
    ingest.start(bus=a, state_dir=tmp_path / "one", ticker=False, catch_up=False)
    ing = ingest.start(bus=a, state_dir=tmp_path / "two", ticker=False, catch_up=False)
    assert ing.state_dir == Path(tmp_path / "two")
    assert len(a.subscribers_for("made_up.thing")) == 1


def test_stop_then_start_subscribes_afresh(stopped, tmp_path):
    a = EventBus()
    ing = ingest.start(bus=a, state_dir=tmp_path, ticker=False, catch_up=False)
    ing.stop()
    assert not ing.started and not a.subscribers_for("made_up.thing")
    ingest.start(bus=a, state_dir=tmp_path, ticker=False, catch_up=False)
    assert len(a.subscribers_for("made_up.thing")) == 1


def test_the_api_contribution_does_not_start_ingest_under_the_test_switch(stopped, monkeypatch):
    """The root conftest sets ``AXIOM_JOURNAL_INGEST=0`` for the whole suite; this pins the
    switch itself, with and without it."""
    pytest.importorskip("fastapi")
    import fastapi

    from axiom.extensions.builtins.journal import api

    monkeypatch.setenv(api.INGEST_ENV, "0")
    api.register_routes(fastapi.APIRouter(prefix="/api/v1"))
    assert not ingest.current().started
    monkeypatch.setenv(api.INGEST_ENV, "1")
    monkeypatch.setattr(
        ingest, "start", lambda **kw: kw.setdefault("marker", True) and ingest.current()
    )
    api.register_routes(fastapi.APIRouter(prefix="/api/v1"))
