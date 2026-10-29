# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fleet console tables (ADR-119, spec-fleet-console §2).

Schema-per-extension per ADR-052: no hardcoded ``schema=`` — the session
provider sets the search path. ``fleet_reports`` is append-only;
``fleet_latest`` is a projection maintained transactionally on ingest.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class FleetNode(Base):
    __tablename__ = "fleet_nodes"

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    site: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(256))
    profile: Mapped[str | None] = mapped_column(String(64))
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {report_kind: declared push cadence in seconds}
    cadences: Mapped[dict | None] = mapped_column(JSON)


class FleetReport(Base):
    __tablename__ = "fleet_reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("fleet_nodes.node_id"), nullable=False, index=True
    )
    site: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    reporter_principal: Mapped[str] = mapped_column(String(256), nullable=False)
    envelope_hash: Mapped[str | None] = mapped_column(String(128))
    # unverified (phase 1 transport-auth only) | verified | failed (phase 2)
    signature_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unverified")


class FleetLatest(Base):
    __tablename__ = "fleet_latest"

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    report_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("fleet_reports.id"), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FleetPin(Base):
    """Declared release state (spec §2/§6; consumed by the drift view, P2).

    Append-only: current = latest row per (site, scope, channel) with
    ``superseded_at`` null. Never pushed to a node — declaration only.
    """

    __tablename__ = "fleet_pins"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False, default="stable")
    declared_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    declared_by: Mapped[str] = mapped_column(String(256), nullable=False)
    declared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
