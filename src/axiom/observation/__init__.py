# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Everything the system learns is an observation (ADR-265): a platform primitive.

An observation is one thing the system learned about the world, in one of three
families:

- a **reading**: a quantity with a unit, from an instrument or a model;
- a **statement**: a claim from a person or a document (a decision, a blocker, a
  note, a signed logbook entry);
- an **event**: something that started, ended or occurred, usually derived from
  other observations (a trigger fired, a threshold was crossed, a feed went stale).

Every observation carries the same fields, so every surface (the web app, the
CLI, MCP tools, chat, notifications) can show it the same way and none has to
guess: what it is about (``subjects``), when it was true and when it was learned
(``observed_at``, ``recorded_at``), who or what produced it and what kind of
evidence it is (``source``, ``source_class``), how sure we are (a reading's
``uncertainty``, an interpreted statement's ``confidence``), what it came from
(``derivation``), and who may see it (``labels``).

This is a contract, not a table (ADR-265 D4): readings stay in silver and gold,
statements and events get their own store, and the journal indexes them. The
rules below are the ones the ADR decided, enforced at construction so a producer
cannot mint an observation the surfaces would have to second-guess:

- ``None`` is never zero. A reading with no reported uncertainty says "not
  reported"; zero would be a claim of perfect precision.
- A model's raw self-score is never shown as confidence. Until an extractor
  version has a calibration, its confidence is "not yet known" (D10).
- A stated observation is attributed to a person instead of carrying a
  confidence. An agent's claim is ``interpreted`` and names the person
  accountable for the agent (D8).
- A modelled or derived observation names its method and version, and a derived
  one names the observations it came from.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

WIRE_SCHEMA = "observation/1"

#: What kind of evidence an observation is. The first four are the data
#: platform's emitter vocabulary for readings; ``derived`` is computed from other
#: observations; ``stated`` is what a person said or signed; ``interpreted`` is
#: what a model read from text, and is never re-read as stated (ADR-265 D8).
SOURCE_CLASSES: tuple[str, ...] = (
    "measured", "estimated", "predicted", "simulated", "derived", "stated", "interpreted",
)
#: Classes that must name the model or method that produced them.
MODELLED_CLASSES: frozenset[str] = frozenset({"estimated", "predicted", "simulated"})

#: ADR-132's CLOSED consumer contract for a reading's quality. It never grows: a
#: consumer switching on it must be able to enumerate it. The producer's open
#: diagnosis goes in ``quality_reason``.
QUALITIES: tuple[str, ...] = ("good", "suspect", "bad", "saturated", "stale")

EVENT_PHASES: tuple[str, ...] = ("started", "ended", "occurred")
SEVERITIES: tuple[str, ...] = ("info", "warning", "critical")

#: Which source classes each family may carry.
_FAMILY_CLASSES: dict[str, frozenset[str]] = {
    "reading": frozenset({"measured", "estimated", "predicted", "simulated", "derived"}),
    "statement": frozenset({"stated", "interpreted"}),
    "event": frozenset({"measured", "derived", "stated", "interpreted"}),
}


class ObservationError(ValueError):
    """An observation outside the contract. Raised at construction, never later."""


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ObservationError(message)


# -- parts -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SubjectRef:
    """What an observation is about, typed: ``channel:…``, ``run:…``, ``person:…``.

    A product registers its own kinds (a facility's ``run`` or ``sample``); the
    contract only requires a kind and an id.
    """

    kind: str
    id: str

    def __post_init__(self) -> None:
        _need(bool(self.kind) and ":" not in self.kind and bool(self.id),
              f"a subject is kind:id, got {self.kind!r}:{self.id!r}")

    @classmethod
    def parse(cls, text: str) -> SubjectRef:
        kind, sep, ident = str(text).partition(":")
        _need(bool(sep), f"a subject is kind:id, got {text!r}")
        return cls(kind, ident)

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass(frozen=True)
class Source:
    """Who or what produced the observation.

    ``producer`` names the instrument, connector, extractor, trigger, agent or
    logbook. ``principal`` is the authenticated identity behind it, and for an
    agent the person accountable for it. ``agent`` names the agent when one
    produced it.
    """

    producer: str
    principal: str = ""
    agent: str = ""

    def __post_init__(self) -> None:
        _need(bool(self.producer), "an observation names what produced it")


@dataclass(frozen=True)
class Derivation:
    """What an observation came from: the observations it read, and how."""

    inputs: tuple[str, ...] = ()
    method: str = ""
    version: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))


@dataclass(frozen=True)
class Confidence:
    """How sure an interpreted statement is (ADR-265 D10).

    Calibration is never a gate: an uncalibrated confidence is carried as "not
    yet known", so a surface shows where the number will appear. A raw model
    self-score is never carried at all.
    """

    value: float | None = None
    calibrated: bool = False
    basis: str = ""

    def __post_init__(self) -> None:
        if not self.calibrated:
            _need(self.value is None,
                  "an uncalibrated confidence is not yet known: a raw self-score is never carried")
            return
        _need(self.value is not None and 0.0 <= float(self.value) <= 1.0,
              "a calibrated confidence is between 0 and 1")
        _need(bool(self.basis), "a calibrated confidence says what it was measured against (basis)")

    @classmethod
    def not_yet_known(cls) -> Confidence:
        return cls()

    def words(self) -> str:
        if not self.calibrated or self.value is None:
            return "confidence not yet known"
        return f"confidence {round(self.value * 100)}%"


# -- the three families ----------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    """A quantity with a unit. ``uncertainty`` is a standard uncertainty in the
    value's unit; ``None`` means the source did not report one, which is a
    different fact from zero."""

    value: float | None
    unit: str | None = None
    quality: str = "good"
    uncertainty: float | None = None
    quality_reason: str | None = None

    family = "reading"

    def __post_init__(self) -> None:
        _need(self.quality in QUALITIES,
              f"a reading's quality is one of {', '.join(QUALITIES)}, not {self.quality!r}")
        _need(self.uncertainty is None or float(self.uncertainty) >= 0.0,
              "an uncertainty is never negative")

    def uncertainty_words(self) -> str:
        return uncertainty_words(self.uncertainty, self.unit)


def uncertainty_words(uncertainty: Any, unit: str | None = None) -> str:
    """How sure a reading is, in the words every surface uses (ADR-265 D7).

    ``uncertainty`` is a bare magnitude (a :class:`Reading`'s), a served bound
    (``axiom.uncertainty.serving.Served`` or its wire payload, the result
    envelope's ``uncertainty``), or ``None``. Absence, and a bound nothing could
    claim, read "not reported": never zero, never a blank cell. A range is
    stated as a range; the envelope keeps the premise for anyone who asks.
    """
    u = f" {unit}" if unit else ""
    if uncertainty is None:
        return "not reported"
    if isinstance(uncertainty, int | float):
        return f"± {float(uncertainty):g}{u}"
    bound = uncertainty.payload() if hasattr(uncertainty, "payload") else uncertainty
    low, high = (bound or {}).get("low"), (bound or {}).get("high")
    if low is None or high is None:
        return "not reported"
    if low == high:
        return f"± {float(low):g}{u}"
    return f"± {float(low):g} to {float(high):g}{u}"


@dataclass(frozen=True)
class Statement:
    """A claim from a person or a document. ``kind`` is the product's vocabulary
    (``decision``, ``blocker``, ``action_item``, a logbook entry type);
    ``claims`` holds structured fields, ``ref`` the authoritative record."""

    kind: str
    text: str
    claims: Mapping[str, Any] = field(default_factory=dict)
    ref: str = ""

    family = "statement"

    def __post_init__(self) -> None:
        _need(bool(self.kind) and bool(self.text), "a statement has a kind and words")
        object.__setattr__(self, "claims", dict(self.claims))


@dataclass(frozen=True)
class Event:
    """Something that started, ended or occurred."""

    kind: str
    phase: str = "occurred"
    severity: str = "info"
    detail: Mapping[str, Any] = field(default_factory=dict)

    family = "event"

    def __post_init__(self) -> None:
        _need(bool(self.kind), "an event has a kind")
        _need(self.phase in EVENT_PHASES,
              f"an event's phase is one of {', '.join(EVENT_PHASES)}, not {self.phase!r}")
        _need(self.severity in SEVERITIES,
              f"an event's severity is one of {', '.join(SEVERITIES)}, not {self.severity!r}")
        object.__setattr__(self, "detail", dict(self.detail))


Content = Reading | Statement | Event


# -- the observation -------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    subjects: tuple[SubjectRef, ...]
    observed_at: datetime
    recorded_at: datetime
    source: Source
    source_class: str
    content: Content
    confidence: Confidence | None = None
    derivation: Derivation | None = None
    labels: tuple[str, ...] = ()
    id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "subjects", tuple(self.subjects))
        object.__setattr__(self, "labels", tuple(self.labels))
        _need(bool(self.subjects), "an observation is about something: name at least one subject")
        for name in ("observed_at", "recorded_at"):
            t = getattr(self, name)
            _need(isinstance(t, datetime) and t.tzinfo is not None,
                  f"{name} carries a time zone")
        _need(self.source_class in SOURCE_CLASSES,
              f"source_class is one of {', '.join(SOURCE_CLASSES)}, not {self.source_class!r}")
        family = self.family
        _need(self.source_class in _FAMILY_CLASSES[family],
              f"a {family} is not {self.source_class!r}; it is one of "
              f"{', '.join(sorted(_FAMILY_CLASSES[family]))}")

        d = self.derivation
        if self.source_class in MODELLED_CLASSES:
            _need(d is not None and bool(d.method),
                  f"a {self.source_class} observation names its model (derivation.method)")
        if self.source_class in ("derived", "interpreted"):
            _need(d is not None and bool(d.method) and bool(d.version),
                  f"a {self.source_class} observation names its method and version (derivation)")
            _need(bool(d.inputs), f"a {self.source_class} observation names what it came from")

        if self.source_class == "stated":
            _need(bool(self.source.principal),
                  "a stated observation is attributed to the person who stated it (principal)")
            _need(self.confidence is None,
                  "a stated observation carries no confidence: it is attributed instead")
        if self.source_class == "interpreted":
            _need(self.confidence is not None,
                  "an interpreted observation carries a confidence, even if not yet known")
        if self.source.agent:
            _need(bool(self.source.principal),
                  "an agent's observation names the person accountable for the agent (principal)")

    @property
    def family(self) -> str:
        return self.content.family


# -- the wire --------------------------------------------------------------------------------


def _iso(t: datetime) -> str:
    return t.isoformat()


def _content_wire(c: Content) -> dict[str, Any]:
    if isinstance(c, Reading):
        return {"value": c.value, "unit": c.unit, "quality": c.quality,
                "uncertainty": c.uncertainty, "quality_reason": c.quality_reason}
    if isinstance(c, Statement):
        return {"kind": c.kind, "text": c.text, "claims": dict(c.claims), "ref": c.ref or None}
    return {"kind": c.kind, "phase": c.phase, "severity": c.severity, "detail": dict(c.detail)}


def to_wire(obs: Observation) -> dict[str, Any]:
    """The one envelope every surface reads. Every key is present, nulls included:
    a reader must never infer absence from a missing key."""
    d = obs.derivation
    return {
        "schema": WIRE_SCHEMA,
        "id": obs.id or None,
        "family": obs.family,
        "subjects": [str(s) for s in obs.subjects],
        "observed_at": _iso(obs.observed_at),
        "recorded_at": _iso(obs.recorded_at),
        "source": {"producer": obs.source.producer, "principal": obs.source.principal or None,
                   "agent": obs.source.agent or None},
        "source_class": obs.source_class,
        "content": _content_wire(obs.content),
        "confidence": None if obs.confidence is None else {
            "value": obs.confidence.value, "calibrated": obs.confidence.calibrated,
            "basis": obs.confidence.basis or None},
        "derivation": None if d is None else {
            "inputs": list(d.inputs), "method": d.method or None, "version": d.version or None,
            "content_hash": d.content_hash or None},
        "labels": list(obs.labels),
    }


def from_wire(payload: Mapping[str, Any]) -> Observation:
    """Read an envelope back, enforcing the contract as construction does."""
    _need(payload.get("schema") == WIRE_SCHEMA,
          f"unknown observation schema {payload.get('schema')!r}; this reader knows {WIRE_SCHEMA}")
    family = payload.get("family")
    c = payload.get("content") or {}
    try:
        content: Content
        if family == "reading":
            content = Reading(value=c.get("value"), unit=c.get("unit"), quality=c.get("quality", "good"),
                              uncertainty=c.get("uncertainty"), quality_reason=c.get("quality_reason"))
        elif family == "statement":
            content = Statement(kind=c["kind"], text=c["text"], claims=c.get("claims") or {},
                                ref=c.get("ref") or "")
        elif family == "event":
            content = Event(kind=c["kind"], phase=c.get("phase", "occurred"),
                            severity=c.get("severity", "info"), detail=c.get("detail") or {})
        else:
            raise ObservationError(f"unknown family {family!r}")
        src = payload.get("source") or {}
        conf = payload.get("confidence")
        der = payload.get("derivation")
        return Observation(
            id=payload.get("id") or "",
            subjects=tuple(SubjectRef.parse(s) for s in payload.get("subjects") or ()),
            observed_at=datetime.fromisoformat(payload["observed_at"]),
            recorded_at=datetime.fromisoformat(payload["recorded_at"]),
            source=Source(producer=src.get("producer") or "", principal=src.get("principal") or "",
                          agent=src.get("agent") or ""),
            source_class=payload.get("source_class", ""),
            content=content,
            confidence=None if conf is None else Confidence(
                value=conf.get("value"), calibrated=bool(conf.get("calibrated")),
                basis=conf.get("basis") or ""),
            derivation=None if der is None else Derivation(
                inputs=tuple(der.get("inputs") or ()), method=der.get("method") or "",
                version=der.get("version") or "", content_hash=der.get("content_hash") or ""),
            labels=tuple(payload.get("labels") or ()),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ObservationError):
            raise
        raise ObservationError(f"not an observation: {exc}") from None


__all__ = [
    "EVENT_PHASES", "MODELLED_CLASSES", "QUALITIES", "SEVERITIES", "SOURCE_CLASSES", "WIRE_SCHEMA",
    "Confidence", "Content", "Derivation", "Event", "Observation", "ObservationError", "Reading",
    "Source", "Statement", "SubjectRef", "from_wire", "to_wire", "uncertainty_words",
]
