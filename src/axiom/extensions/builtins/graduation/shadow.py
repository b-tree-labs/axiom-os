# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shadow-mode outcome recording for the LLM tier classifier (phase 1).

This module observes every :class:`~axiom.llm.router.RoutingDecision` the
platform's tier classifier produces and appends one outcome record per
decision to a local, locked, append-only JSONL log. It changes nothing:

- The live routing result is never altered — observation happens in the
  router's best-effort zone and can never raise into the caller.
- Nothing leaves the machine — the SDK backend is constructed with an
  explicit ``NullEmitter`` and file-based storage; the fallback backend is
  plain local JSONL via the platform's ``locked_append_jsonl``.
- No content is recorded — decision *shapes* only (tier, classifier stage,
  basis, sensitivity, booleans, counts). Never message text, never the
  matched keyword terms, never reason strings. Decisions, not data.

Backends
--------
When the optional ``postrule`` SDK is installed (``pip install
'axiom-os-lm[graduation]'``), the recorder wraps the mirror rule with
``@ml_switch`` pinned to ``Phase.RULE`` (``phase_limit=RULE`` — the switch
can never advance) and records verdicts through the SDK's rotating,
flock-guarded ``FileStorage``. Without the SDK, a thin recorder writes the
identical ``ClassificationRecord`` row shape to the same path with
``locked_append_jsonl``, so the log is continuous across either backend.

Enablement
----------
On by default in real processes; ``AXIOM_GRADUATION_SHADOW`` (0/1) is the
explicit override, the ``graduation.shadow`` setting the persistent one,
and recording is automatically disabled under pytest so test-suite
decisions never taint the dogfood outcome log.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from axiom.llm.router import RoutingDecision

#: The one shadowed call site (phase 1): the router's tier decision.
SWITCH_NAME = "llm_tier_routing"

#: Lifecycle phase of the shadowed switch — the deterministic rule is (and
#: stays) the decision-maker; this module only accumulates the outcome log.
PHASE = "RULE"

#: The closed label set the tier classifier emits (RoutingTier values).
LABELS = ("public", "export_controlled")

AUTHOR = "@graduation:axiom"

#: The exact feature fields recorded per decision — the whole shape, and
#: nothing else. Content (message text, matched terms, reason strings)
#: must never appear here.
SHAPE_FIELDS = (
    "phase",
    "session_mode",
    "sensitivity",
    "context_turns",
    "classifier",
    "basis",
    "keyword_matched",
    "slm_tier",
    "failure_reason",
)

_ENV_TOGGLE = "AXIOM_GRADUATION_SHADOW"
_FALSEY = ("", "0", "false", "no", "off")

_RECORDER: Any | None = None
_INSTALLED = False


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def outcomes_dir() -> Path:
    """Base directory for outcome logs: ``<user state dir>/graduation/outcomes``."""
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "graduation" / "outcomes"


def outcome_log_path() -> Path:
    """The active outcome-log segment for the shadowed switch."""
    return outcomes_dir() / SWITCH_NAME / "outcomes.jsonl"


# ---------------------------------------------------------------------------
# The mirror rule + decision shape
# ---------------------------------------------------------------------------


def tier_policy(shape: dict[str, Any]) -> str:
    """Deterministic mirror of the router's tier policy over a decision shape.

    Reproduces the documented pipeline (session override → keyword →
    SLM verdict → sensitivity fallback) from content-free features. The
    incumbent decision is the reference: a mismatch between this mirror and
    the live tier is recorded as ``incorrect`` and means the mirror (or the
    shape) has drifted from the router — a signal we want in the log.
    """
    if shape.get("classifier") == "session":
        mode = shape.get("session_mode")
        return mode if mode in LABELS else "public"
    if shape.get("keyword_matched"):
        return "export_controlled"
    if shape.get("classifier") == "ollama":
        slm = shape.get("slm_tier")
        if slm in LABELS:
            return str(slm)
    return "export_controlled" if shape.get("sensitivity") == "strict" else "public"


def build_shape(decision: RoutingDecision, features: dict[str, Any]) -> dict[str, Any]:
    """Project a RoutingDecision onto the content-free feature shape.

    Whitelist projection: only :data:`SHAPE_FIELDS` are emitted. The
    decision's ``matched_terms`` / ``keyword_term`` (the sensitive keywords
    themselves) and free-text ``reason`` are deliberately reduced to a
    boolean and enum-valued fields.
    """
    failure = decision.classifier_failure
    return {
        "phase": PHASE,
        "session_mode": str(features.get("session_mode", "auto")),
        "sensitivity": str(features.get("sensitivity", "balanced")),
        "context_turns": int(features.get("context_turns", 0) or 0),
        "classifier": decision.classifier,
        "basis": decision.basis,
        "keyword_matched": bool(decision.matched_terms),
        "slm_tier": decision.tier.value if decision.classifier == "ollama" else None,
        "failure_reason": failure.reason if failure is not None else None,
    }


# ---------------------------------------------------------------------------
# Recorders — postrule SDK when installed, locked JSONL otherwise
# ---------------------------------------------------------------------------


def _import_postrule():
    """Return the postrule module, or None when unusable.

    Defensive on purpose: a broken editable install can leave a bare
    namespace package that imports but has no API. Missing SDK is a
    supported mode (the ``[graduation]`` extra is optional), never an error.
    """
    try:
        import postrule
        import postrule.telemetry  # noqa: F401  (needed for NullEmitter)

        if not hasattr(postrule, "ml_switch"):
            return None
        return postrule
    except Exception:
        return None


class _PostruleRecorder:
    """SDK-backed recorder: ``@ml_switch`` mirror pinned to Phase.RULE.

    ``telemetry=NullEmitter()`` guarantees no hosted service is ever
    called; ``FileStorage`` keeps the log local with rotation + flock.
    """

    backend = "postrule"

    def __init__(self, pr: Any) -> None:
        decorate = pr.ml_switch(
            labels=list(LABELS),
            name=SWITCH_NAME,
            author=AUTHOR,
            project="axiom",
            starting_phase=pr.Phase.RULE,
            phase_limit=pr.Phase.RULE,  # phase 1: the switch can never advance
            auto_record=False,
            auto_advance=False,
            storage=pr.FileStorage(outcomes_dir()),
            telemetry=pr.telemetry.NullEmitter(),
        )
        self._switch = decorate(tier_policy)

    def record(
        self, *, shape: dict[str, Any], predicted: str, chosen: str, outcome: str
    ) -> None:
        self._switch.record_verdict(
            input=shape, label=chosen, outcome=outcome, source="rule"
        )


class _JsonlRecorder:
    """Fallback recorder when the optional SDK is absent.

    Writes the identical ``ClassificationRecord`` row shape to the same
    path via the platform's canonical ``locked_append_jsonl``, so a later
    SDK install continues the same log.
    """

    backend = "jsonl"

    def record(
        self, *, shape: dict[str, Any], predicted: str, chosen: str, outcome: str
    ) -> None:
        from axiom.infra.state import locked_append_jsonl

        locked_append_jsonl(
            outcome_log_path(),
            {
                "timestamp": time.time(),
                "input": shape,
                "label": chosen,
                "outcome": outcome,
                "source": "rule",
                "confidence": 1.0,
                "rule_output": predicted,
                "model_output": None,
                "model_confidence": None,
                "ml_output": None,
                "ml_confidence": None,
                "model_outcome": None,
                "ml_outcome": None,
                "action_result": None,
                "action_raised": None,
                "action_elapsed_ms": None,
            },
        )


def _get_recorder() -> Any:
    global _RECORDER
    if _RECORDER is None:
        pr = _import_postrule()
        _RECORDER = _PostruleRecorder(pr) if pr is not None else _JsonlRecorder()
    return _RECORDER


# ---------------------------------------------------------------------------
# The observer
# ---------------------------------------------------------------------------


def observe_routing_decision(
    decision: RoutingDecision, features: dict[str, Any]
) -> None:
    """Record one outcome per routing decision. Never raises."""
    try:
        shape = build_shape(decision, features)
        predicted = tier_policy(shape)
        chosen = str(decision.tier.value)
        outcome = "correct" if predicted == chosen else "incorrect"
        _get_recorder().record(
            shape=shape, predicted=predicted, chosen=chosen, outcome=outcome
        )
    except Exception:
        # Shadow observation must never affect (or slow) live routing.
        pass


def shadow_enabled() -> bool:
    """Whether shadow recording is on for this process.

    Precedence: ``AXIOM_GRADUATION_SHADOW`` env (explicit override) →
    pytest guard (never taint the dogfood log from test runs) →
    ``graduation.shadow`` setting → default ON.
    """
    env = os.environ.get(_ENV_TOGGLE)
    if env is not None:
        return env.strip().lower() not in _FALSEY
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        value = SettingsStore().get("graduation.shadow", True)
    except Exception:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in _FALSEY
    return bool(value)


def auto_install() -> None:
    """Register the observer with the router (idempotent; enablement-gated).

    Called lazily by the router's observer bootstrap on the first
    classification of the process, so every surface that routes (chat,
    ingress bridge, MCP dispatch, web tools) accumulates the same log with
    zero per-surface wiring.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    if not shadow_enabled():
        return
    from axiom.llm.router import register_decision_observer

    register_decision_observer(observe_routing_decision)
    _INSTALLED = True


def uninstall() -> None:
    """Remove the observer (used by tests and operator tooling)."""
    global _INSTALLED
    from axiom.llm.router import unregister_decision_observer

    unregister_decision_observer(observe_routing_decision)
    _INSTALLED = False


def reset_for_tests() -> None:
    """Drop cached recorder/installation state (test isolation only)."""
    global _RECORDER, _INSTALLED
    _RECORDER = None
    _INSTALLED = False


# ---------------------------------------------------------------------------
# Reading the log (status surface)
# ---------------------------------------------------------------------------


def load_outcome_records() -> list[dict[str, Any]]:
    """Load every outcome record, oldest first (rotated segments + active).

    Malformed lines are skipped — some data beats no data.
    """
    active = outcome_log_path()
    directory = active.parent
    if not directory.is_dir():
        return []
    rotated = sorted(
        (
            p
            for p in directory.glob("outcomes.jsonl.*")
            if p.suffix.lstrip(".").isdigit()
        ),
        key=lambda p: int(p.suffix.lstrip(".")),
        reverse=True,  # highest number = oldest segment
    )
    records: list[dict[str, Any]] = []
    for path in [*rotated, active]:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return records


__all__ = [
    "AUTHOR",
    "LABELS",
    "PHASE",
    "SHAPE_FIELDS",
    "SWITCH_NAME",
    "auto_install",
    "build_shape",
    "load_outcome_records",
    "observe_routing_decision",
    "outcome_log_path",
    "outcomes_dir",
    "shadow_enabled",
    "tier_policy",
    "uninstall",
]
