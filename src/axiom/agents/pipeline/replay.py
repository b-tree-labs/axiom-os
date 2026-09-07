# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ReplayEnvelope — capture/restore primitives for ADR-034 §D9.

Replay determinism is a *design property*. Every plan derivation or agent step
that produces persistent memory carries a `ReplayEnvelope` recording what the
runtime captured and what it explicitly couldn't (the `not_captured` list).

Modes:
    BEST_EFFORT (default) — capture what we can; declare gaps.
    DETERMINISTIC_STRICT  — every gap declared `severity="blocker"` causes
                            `build()` to raise; the run is rejected.

The fingerprint is `sha256(canonical_json(captured))`. Two envelopes with the
same captured inputs produce the same fingerprint regardless of insertion
order, dict key order, or list-vs-tuple representation.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Enums + dataclasses
# ---------------------------------------------------------------------------

class ReplayMode(str, Enum):
    """Replay capture mode (ADR-034 §D9 / analysis §10.3)."""

    BEST_EFFORT = "best_effort"
    DETERMINISTIC_STRICT = "deterministic_strict"


_VALID_SEVERITIES: tuple[str, ...] = ("informational", "warning", "blocker")


@dataclass(frozen=True)
class CapturedInput:
    """One input field captured into the envelope."""

    name: str
    value: Any  # JSON-serializable; validated at capture time.
    captured_at: datetime
    source: str  # "config" | "context" | "tool_output" | etc.


@dataclass(frozen=True)
class UncapturedInput:
    """A declared gap in the envelope."""

    name: str
    reason: str
    severity: Literal["informational", "warning", "blocker"]


@dataclass(frozen=True)
class ReplayEnvelope:
    """Envelope recording captured + uncaptured inputs for a step / derivation."""

    envelope_id: str
    mode: ReplayMode
    captured: tuple[CapturedInput, ...]
    not_captured: tuple[UncapturedInput, ...]
    fingerprint: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------

def _canonicalize(value: Any) -> Any:
    """Reduce a value to a canonical, deterministic JSON representation.

    - Dicts: keys sorted lexicographically; values recursively canonicalized.
    - Tuples / lists: list with each element canonicalized (tuple == list).
    - Floats: handled at JSON encode time (allow_nan=False, default sort_keys).
    - Datetimes: ISO-8601 UTC string.
    - Bytes: hex string.
    - None / bool / int / str: passthrough.
    - Other types: TypeError raised at capture time, never reach here.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"non-finite float not canonicalizable: {value!r}")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, Mapping):
        return {str(k): _canonicalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    raise TypeError(
        f"value of type {type(value).__name__} is not JSON-canonicalizable; "
        "capture only primitives, datetimes, bytes, mappings, or sequences"
    )


def _validate_serializable(name: str, value: Any) -> None:
    """Raise immediately if `value` cannot be canonicalized + JSON-encoded."""
    try:
        canonical = _canonicalize(value)
        json.dumps(canonical, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise TypeError(
            f"capture({name!r}) value is not JSON-serializable: {e}"
        ) from e


def _canonical_json_for_captures(captured: Sequence[CapturedInput]) -> str:
    """Build the canonical JSON over which the fingerprint is computed.

    Captures are emitted as a list sorted by `name`, then for each entry only
    `name` + canonicalized `value` are included. `captured_at` and `source`
    are excluded from the fingerprint so timestamp drift + provenance hints
    don't break replay equivalence.
    """
    payload = [
        {"name": c.name, "value": _canonicalize(c.value)}
        for c in sorted(captured, key=lambda c: c.name)
    ]
    return json.dumps(
        payload,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def _fingerprint(captured: Sequence[CapturedInput]) -> str:
    """sha256 hex of canonical JSON over the captured set."""
    canonical = _canonical_json_for_captures(captured)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class EnvelopeBuilder:
    """Fluent builder. Use `.capture(...)` and `.declare_gap(...)` then `.build()`."""

    def __init__(self, mode: ReplayMode = ReplayMode.BEST_EFFORT) -> None:
        self._mode = mode
        self._captured: list[CapturedInput] = []
        self._not_captured: list[UncapturedInput] = []

    @property
    def mode(self) -> ReplayMode:
        return self._mode

    def capture(
        self,
        name: str,
        value: Any,
        *,
        source: str = "context",
    ) -> EnvelopeBuilder:
        """Capture a single input. Raises if `value` isn't JSON-serializable."""
        if not isinstance(name, str) or not name:
            raise ValueError("capture name must be a non-empty string")
        _validate_serializable(name, value)
        self._captured.append(
            CapturedInput(
                name=name,
                value=value,
                captured_at=datetime.now(UTC),
                source=source,
            )
        )
        return self

    def declare_gap(
        self,
        name: str,
        reason: str,
        severity: str = "informational",
    ) -> EnvelopeBuilder:
        """Declare an input the runtime cannot capture."""
        if severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {_VALID_SEVERITIES}, got {severity!r}"
            )
        self._not_captured.append(
            UncapturedInput(name=name, reason=reason, severity=severity)  # type: ignore[arg-type]
        )
        return self

    def build(self) -> ReplayEnvelope:
        """Finalize. In strict mode, raises if any gap is severity=blocker."""
        if self._mode is ReplayMode.DETERMINISTIC_STRICT:
            blockers = [g for g in self._not_captured if g.severity == "blocker"]
            if blockers:
                names = ", ".join(g.name for g in blockers)
                raise ValueError(
                    f"deterministic_strict envelope cannot be built: "
                    f"blocker gap(s) declared: {names}"
                )
        captured = tuple(self._captured)
        not_captured = tuple(self._not_captured)
        return ReplayEnvelope(
            envelope_id=uuid.uuid4().hex,
            mode=self._mode,
            captured=captured,
            not_captured=not_captured,
            fingerprint=_fingerprint(captured),
            created_at=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# Common capture helpers
# ---------------------------------------------------------------------------

def capture_model_call(
    builder: EnvelopeBuilder,
    *,
    provider: str,
    model: str,
    temperature: float,
    system_prompt: str | None,
    user_prompt: str,
) -> EnvelopeBuilder:
    """Capture a model call's deterministic inputs."""
    builder.capture("model.provider", provider, source="config")
    builder.capture("model.model", model, source="config")
    builder.capture("model.temperature", float(temperature), source="config")
    builder.capture("model.system_prompt", system_prompt, source="context")
    builder.capture("model.user_prompt", user_prompt, source="context")
    return builder


def capture_retrieval(
    builder: EnvelopeBuilder,
    *,
    query: str,
    fragment_ids: Sequence[str],
    scores: Sequence[float],
) -> EnvelopeBuilder:
    """Capture a retrieval set used to ground a step."""
    builder.capture("retrieval.query", query, source="context")
    builder.capture(
        "retrieval.fragment_ids", list(fragment_ids), source="tool_output"
    )
    builder.capture(
        "retrieval.scores", [float(s) for s in scores], source="tool_output"
    )
    return builder


def capture_tool_invocation(
    builder: EnvelopeBuilder,
    *,
    tool_id: str,
    tool_version: str,
    input_args: Mapping[str, Any],
) -> EnvelopeBuilder:
    """Capture a deterministic tool call's inputs."""
    builder.capture("tool.id", tool_id, source="config")
    builder.capture("tool.version", tool_version, source="config")
    builder.capture("tool.input_args", dict(input_args), source="context")
    return builder


# ---------------------------------------------------------------------------
# Replay verification
# ---------------------------------------------------------------------------

def envelopes_equivalent(
    a: ReplayEnvelope,
    b: ReplayEnvelope,
    *,
    ignore_timestamps: bool = True,
) -> tuple[bool, str]:
    """Compare two envelopes for replay equivalence.

    Returns (ok, diff_explanation). `diff_explanation` is empty when ok is True.

    Equivalence is defined by:
    - Same `mode`.
    - Same `fingerprint` (i.e. canonical captured set matches).
    - Same `not_captured` set (by name/reason/severity tuple, order-insensitive).

    When `ignore_timestamps=True` (the default), `captured_at` + `created_at`
    are ignored. The fingerprint already excludes timestamps, so this only
    affects strict structural compare paths.
    """
    if a.mode is not b.mode:
        return False, f"mode mismatch: {a.mode.value} vs {b.mode.value}"

    if a.fingerprint != b.fingerprint:
        # Provide a useful diff: which captured names / values differ.
        a_map = {c.name: _canonicalize(c.value) for c in a.captured}
        b_map = {c.name: _canonicalize(c.value) for c in b.captured}
        only_a = sorted(set(a_map) - set(b_map))
        only_b = sorted(set(b_map) - set(a_map))
        diffs: list[str] = []
        if only_a:
            diffs.append(f"captured only in A: {only_a}")
        if only_b:
            diffs.append(f"captured only in B: {only_b}")
        for name in sorted(set(a_map) & set(b_map)):
            if a_map[name] != b_map[name]:
                diffs.append(
                    f"captured value differs for {name!r}: "
                    f"{a_map[name]!r} vs {b_map[name]!r}"
                )
        msg = "fingerprint mismatch: " + "; ".join(diffs) if diffs else (
            "fingerprint mismatch (no per-field diff identifiable)"
        )
        return False, msg

    a_gaps = {(g.name, g.reason, g.severity) for g in a.not_captured}
    b_gaps = {(g.name, g.reason, g.severity) for g in b.not_captured}
    if a_gaps != b_gaps:
        only_a = sorted(a_gaps - b_gaps)
        only_b = sorted(b_gaps - a_gaps)
        parts: list[str] = []
        if only_a:
            parts.append(f"not_captured only in A: {only_a}")
        if only_b:
            parts.append(f"not_captured only in B: {only_b}")
        return False, "gap mismatch: " + "; ".join(parts)

    if not ignore_timestamps:
        if a.created_at != b.created_at:
            return False, f"created_at differs: {a.created_at} vs {b.created_at}"
        a_ts = tuple(c.captured_at for c in a.captured)
        b_ts = tuple(c.captured_at for c in b.captured)
        if a_ts != b_ts:
            return False, "captured_at timestamps differ"

    return True, ""


__all__ = [
    "ReplayMode",
    "CapturedInput",
    "UncapturedInput",
    "ReplayEnvelope",
    "EnvelopeBuilder",
    "capture_model_call",
    "capture_retrieval",
    "capture_tool_invocation",
    "envelopes_equivalent",
]
