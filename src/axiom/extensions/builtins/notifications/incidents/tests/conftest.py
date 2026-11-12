# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures: an incident store on in-memory SQLite, a settable clock,
and a delivery seam that records what a person would have been sent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.notifications.incidents.store import IncidentStore
from axiom.extensions.builtins.notifications.db_models import Base

T0 = datetime(2026, 11, 10, 2, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> datetime:
        self.t = self.t + timedelta(**kw)
        return self.t


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine):
    return IncidentStore(engine=engine)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sent():
    """What the stage delivered: (incident, rendered) pairs."""
    return []
