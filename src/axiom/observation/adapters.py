# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The records that exist today, presented as observations (ADR-265 step 2).

Each adapter reads its record by shape (a mapping or an object with the named
fields), so this primitive imports no extension. The records stay where they
are: an adapter is a view, not a migration.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from . import (
    Derivation,
    Event,
    Observation,
    ObservationError,
    Reading,
    Source,
    SubjectRef,
)

#: silver's column once defaulted to 'ok', outside ADR-132's vocabulary; it
#: means the row was not judged bad.
_LEGACY_QUALITY = {"ok": "good", "uncertain": "suspect"}


def _get(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _time(value: Any, what: str) -> datetime:
    if isinstance(value, datetime):
        t = value
    elif isinstance(value, str) and value:
        t = datetime.fromisoformat(value)
    else:
        raise ObservationError(f"{what} has no time")
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def from_silver_row(row: Any, *, recorded_at: datetime | None = None) -> Observation:
    """A row of ``silver.signals`` (or ``gold.signals``) is a reading."""
    site, feed, channel = _get(row, "site"), _get(row, "feed"), _get(row, "channel")
    observed = _time(_get(row, "ts"), "the row")
    source_class = _get(row, "source_class") or "measured"
    model_ref = _get(row, "model_ref")
    quality = _get(row, "quality") or "good"
    derivation = None
    if model_ref or source_class != "measured":
        derivation = Derivation(method=model_ref or str(_get(row, "derivation") or ""),
                                version=str(_get(row, "schema_ref") or ""))
    row_hash = _get(row, "row_hash")
    return Observation(
        id=f"reading:{row_hash}/{channel}" if row_hash else "",
        subjects=(SubjectRef("channel", f"{site}/{feed}/{channel}"),),
        observed_at=observed,
        recorded_at=recorded_at or observed,
        source=Source(producer=f"feed:{site}/{feed}"),
        source_class=source_class,
        content=Reading(
            value=_get(row, "value"),
            unit=_get(row, "unit"),
            quality=_LEGACY_QUALITY.get(quality, quality),
            uncertainty=_get(row, "uncertainty"),
            quality_reason=_get(row, "quality_reason"),
        ),
        derivation=derivation,
    )


def from_trigger_event(event: Any, *, recorded_at: datetime | None = None) -> Observation:
    """A video trigger's ``TriggerEvent`` (ADR-240, ADR-264) is a derived event."""
    stream, trigger = _get(event, "stream"), str(_get(event, "trigger") or "")
    name, _, version = trigger.partition("@")
    observed = _time(_get(event, "ts"), "the trigger event")
    detail = dict(_get(event, "detail") or {})
    score = _get(event, "score")
    if score is not None:
        detail["score"] = score
    key = _get(event, "incident_key")
    return Observation(
        subjects=(SubjectRef("stream", stream), SubjectRef("episode", key)),
        observed_at=observed,
        recorded_at=recorded_at or observed,
        source=Source(producer=f"trigger:{trigger}"),
        source_class="derived",
        content=Event(kind=f"video.{name}", phase=_get(event, "kind"),
                      severity=_get(event, "severity") or "info", detail=detail),
        derivation=Derivation(inputs=(f"stream:{stream}@{observed.isoformat()}",),
                              method=f"trigger:{name}", version=version or "unversioned"),
        labels=(str(_get(event, "sensitivity")),) if _get(event, "sensitivity") else (),
    )


def from_cause(cause: Any, *, agent: str, principal: str,
               recorded_at: datetime | None = None) -> Observation:
    """An incident's ``Cause`` (ADR-252) is an event an agent derived; it points at
    the record that holds the evidence."""
    observed = _time(_get(cause, "occurred_at"), "the cause")
    evidence = _get(cause, "evidence_ref") or ""
    subject = _get(cause, "subject") or ""
    area, kind = _get(cause, "area"), _get(cause, "kind")
    return Observation(
        subjects=(SubjectRef(str(area or "platform"), str(subject or kind)),),
        observed_at=observed,
        recorded_at=recorded_at or observed,
        source=Source(producer=f"agent:{agent}", principal=principal, agent=agent),
        source_class="derived",
        content=Event(kind=f"{area}.{kind}", phase="occurred", severity="warning",
                      detail={"action": _get(cause, "action"),
                              "error_class": _get(cause, "error_class")}),
        derivation=Derivation(inputs=(evidence,) if evidence else (f"cause:{area}.{kind}",),
                              method=f"detect:{_get(cause, 'action')}", version="1"),
    )


__all__ = ["from_cause", "from_silver_row", "from_trigger_event"]
