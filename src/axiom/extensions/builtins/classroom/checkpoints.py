# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Checkpoint configuration — FW-1 P3a.

Formalizes course checkpoints (baseline / midpoint / final by default)
as assessment milestones configurable in the CourseManifest. Per
project_course_checkpoints memory:

- Defaults apply when a manifest specifies no ``checkpoints`` field.
- An explicit empty list is a deliberate opt-out — never re-injected.
- Timing accepts either a keyword (``enrollment_complete``,
  ``course_start``, ``midway``, ``course_end``) OR an ISO-8601 date
  string. The validator disambiguates.
- Methods: ``quiz``, ``questionnaire``, ``portfolio``, ``observation``,
  ``none``.

Driven by:

- ``axi classroom prep checkpoints {list|add|remove|skip-defaults}``
- ``classroom_prep_configure_checkpoints`` chat tool
- Default-injection at manifest-load time

The shape matches what Canvas-assignment creation (future phase) will
consume: each checkpoint maps to one assignment with a due date and
a questionnaire reference.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KEYWORD_TIMINGS: frozenset[str] = frozenset(
    {"enrollment_complete", "course_start", "midway", "course_end"}
)

VALID_METHODS: frozenset[str] = frozenset(
    {"quiz", "questionnaire", "portfolio", "observation", "none"}
)


DEFAULT_CHECKPOINTS: list[dict[str, Any]] = [
    {
        "id": "baseline",
        "label": "Baseline Assessment",
        "timing": "enrollment_complete",
        "method": "quiz",
        "questionnaire_id": "",
        "required": True,
    },
    {
        "id": "midpoint",
        "label": "Midpoint Assessment",
        "timing": "midway",
        "method": "quiz",
        "questionnaire_id": "",
        "required": True,
    },
    {
        "id": "final",
        "label": "Final Assessment",
        "timing": "course_end",
        "method": "quiz",
        "questionnaire_id": "",
        "required": True,
    },
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Checkpoint:
    """One configured checkpoint on a course."""

    id: str
    label: str = ""
    timing: str = ""
    method: str = "quiz"
    questionnaire_id: str = ""
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def validate_checkpoint_timing(timing: Any) -> bool:
    """Return True if ``timing`` is a recognized keyword or ISO-8601 date."""
    if not isinstance(timing, str) or not timing:
        return False
    if timing in KEYWORD_TIMINGS:
        return True
    # ISO-8601: ``fromisoformat`` handles YYYY-MM-DD and full timestamps
    # with offset. ``Z`` suffix not supported by Python <3.11, so
    # normalize it.
    candidate = timing.replace("Z", "+00:00") if timing.endswith("Z") else timing
    try:
        datetime.fromisoformat(candidate)
        return True
    except ValueError:
        return False


def parse_checkpoint(data: dict[str, Any]) -> Checkpoint:
    """Build a Checkpoint from a dict, applying sensible defaults + validating.

    Defaults:
    - ``label`` falls back to ``id`` so instructors don't have to type both.
    - ``method`` defaults to ``quiz``.
    - ``required`` defaults to False for newly-added checkpoints (the
      three shipped defaults explicitly set required=True).

    Raises:
    - ValueError on missing ``id``.
    - ValueError on invalid ``timing`` or ``method``.
    """
    cp_id = data.get("id", "")
    if not cp_id:
        raise ValueError("checkpoint dict missing required field: id")

    timing = data.get("timing", "")
    if timing and not validate_checkpoint_timing(timing):
        raise ValueError(
            f"invalid timing {timing!r}: expected a keyword "
            f"({sorted(KEYWORD_TIMINGS)}) or ISO-8601 date"
        )

    method = data.get("method", "quiz")
    if method not in VALID_METHODS:
        raise ValueError(
            f"invalid method {method!r}: expected one of {sorted(VALID_METHODS)}"
        )

    return Checkpoint(
        id=cp_id,
        label=data.get("label") or cp_id,
        timing=timing,
        method=method,
        questionnaire_id=data.get("questionnaire_id", ""),
        required=bool(data.get("required", False)),
    )


# ---------------------------------------------------------------------------
# Default injection
# ---------------------------------------------------------------------------


def apply_default_checkpoints(manifest: dict[str, Any]) -> None:
    """Inject DEFAULT_CHECKPOINTS into ``manifest`` when absent.

    Mutates in place. Semantics:

    - Missing ``checkpoints`` key → inject defaults (deep-copied).
    - Present but empty list → respected as opt-out; nothing changes.
    - Present with content → preserve as-is, don't merge.
    """
    if "checkpoints" not in manifest:
        # Deep-copy via explicit dict() over each default so callers can
        # mutate their injected checkpoints without tainting the template.
        manifest["checkpoints"] = [dict(cp) for cp in DEFAULT_CHECKPOINTS]


# ---------------------------------------------------------------------------
# Course-manifest checkpoint ops (used by CLI + chat tool)
# ---------------------------------------------------------------------------


def list_checkpoints(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return list(manifest.get("checkpoints") or [])


def add_checkpoint(
    manifest: dict[str, Any], checkpoint: dict[str, Any],
) -> dict[str, Any]:
    """Validate + append a checkpoint to the manifest. Returns stored dict."""
    cp = parse_checkpoint(checkpoint)
    existing = manifest.setdefault("checkpoints", [])
    # De-duplicate by id — an add with an existing id REPLACES in place,
    # which is the natural UX for "I want to update this checkpoint".
    out = cp.to_dict()
    for i, old in enumerate(existing):
        if old.get("id") == cp.id:
            existing[i] = out
            return out
    existing.append(out)
    return out


def remove_checkpoint(manifest: dict[str, Any], checkpoint_id: str) -> bool:
    """Remove a checkpoint by id. Returns True if removed, False if absent."""
    existing = manifest.get("checkpoints")
    if not existing:
        return False
    kept = [cp for cp in existing if cp.get("id") != checkpoint_id]
    if len(kept) == len(existing):
        return False
    manifest["checkpoints"] = kept
    return True


def skip_defaults(manifest: dict[str, Any]) -> None:
    """Mark the manifest as having no checkpoints (explicit opt-out)."""
    manifest["checkpoints"] = []


def restore_defaults(manifest: dict[str, Any]) -> None:
    """Reset checkpoints to the shipped defaults."""
    manifest["checkpoints"] = [dict(cp) for cp in DEFAULT_CHECKPOINTS]
