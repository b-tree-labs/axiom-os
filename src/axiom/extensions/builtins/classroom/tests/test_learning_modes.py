# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the learning-mode registry + classroom policy.

Master Educator's design review framed modes as first-class policy
objects: each mode bundles an LLM constraint, a retrieval policy,
a system-prompt overlay, and metadata. The instructor owns the
policy (which modes are allowed + optional forced override);
students pick within that envelope.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.learning_modes import (
    ClassroomModePolicy,
    LearningMode,
    effective_mode,
    get_mode,
    list_modes,
)

# ---------------------------------------------------------------------------
# Registry — modes are data, not code branches
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_required_modes_are_registered(self):
        names = {m.name for m in list_modes()}
        assert {"ask", "tutor", "quiz", "reflect"} <= names

    def test_get_known_mode_returns_mode(self):
        mode = get_mode("tutor")
        assert isinstance(mode, LearningMode)
        assert mode.name == "tutor"

    def test_get_unknown_mode_raises(self):
        with pytest.raises(KeyError):
            get_mode("does-not-exist")

    def test_every_mode_has_description(self):
        for m in list_modes():
            assert m.description, f"mode {m.name} has no description"

    def test_every_mode_declares_llm_constraint(self):
        valid = {"unrestricted", "socratic", "none", "summary-only"}
        for m in list_modes():
            assert m.llm_constraint in valid, (
                f"mode {m.name}: invalid llm_constraint {m.llm_constraint}"
            )

    def test_every_mode_declares_retrieval_policy(self):
        valid = {"full", "peek", "none"}
        for m in list_modes():
            assert m.retrieval_policy in valid, (
                f"mode {m.name}: invalid retrieval_policy {m.retrieval_policy}"
            )


# ---------------------------------------------------------------------------
# Mode-specific invariants (MasterEd review)
# ---------------------------------------------------------------------------


class TestModeInvariants:
    def test_tutor_is_socratic(self):
        tutor = get_mode("tutor")
        assert tutor.llm_constraint == "socratic"
        # System prompt must explicitly forbid direct answers.
        assert "not" in tutor.system_prompt_overlay.lower()
        # Should mention questions or guide.
        assert (
            "question" in tutor.system_prompt_overlay.lower()
            or "guide" in tutor.system_prompt_overlay.lower()
        )

    def test_quiz_has_no_llm_and_no_retrieval(self):
        """Closed-book means closed-book. Quiz mode must NOT let the
        LLM help or let the index leak answers."""
        quiz = get_mode("quiz")
        assert quiz.llm_constraint == "none"
        assert quiz.retrieval_policy == "none"
        assert quiz.student_writes_first is True

    def test_reflect_is_student_writes_first(self):
        reflect = get_mode("reflect")
        assert reflect.student_writes_first is True
        assert reflect.retrieval_policy == "none"

    def test_ask_is_unrestricted(self):
        ask = get_mode("ask")
        assert ask.llm_constraint == "unrestricted"
        assert ask.retrieval_policy == "full"


# ---------------------------------------------------------------------------
# Classroom policy — instructor controls
# ---------------------------------------------------------------------------


class TestPolicyDefaults:
    def test_default_policy_allows_all_registered_modes(self):
        policy = ClassroomModePolicy.default()
        for m in list_modes():
            assert policy.is_allowed(m.name)

    def test_default_policy_has_no_forced_mode(self):
        policy = ClassroomModePolicy.default()
        assert policy.forced_mode is None


class TestPolicyEnforcement:
    def test_disallowed_mode_is_blocked(self):
        policy = ClassroomModePolicy(
            allowed_modes={"ask", "reflect"}, forced_mode=None,
        )
        assert policy.is_allowed("ask")
        assert not policy.is_allowed("tutor")

    def test_forced_mode_overrides_student_preference(self):
        policy = ClassroomModePolicy(
            allowed_modes={"ask", "tutor", "quiz"},
            forced_mode="quiz",
        )
        # Student says they want "ask", but instructor forced "quiz".
        assert effective_mode(policy=policy, student_preference="ask") == "quiz"

    def test_student_preference_respected_within_allowed_set(self):
        policy = ClassroomModePolicy(
            allowed_modes={"ask", "tutor"},
            forced_mode=None,
        )
        assert effective_mode(policy=policy, student_preference="tutor") == "tutor"

    def test_student_picks_disallowed_mode_falls_back_to_default(self):
        policy = ClassroomModePolicy(
            allowed_modes={"ask", "reflect"},
            forced_mode=None,
        )
        # Student wants "quiz" but instructor disabled it — fall back to
        # the safest mode that IS allowed (first in allowed list, or ask).
        out = effective_mode(policy=policy, student_preference="quiz")
        assert out in {"ask", "reflect"}

    def test_no_preference_uses_ask_if_allowed(self):
        policy = ClassroomModePolicy.default()
        assert effective_mode(policy=policy, student_preference=None) == "ask"

    def test_no_preference_and_ask_blocked_uses_first_allowed(self):
        policy = ClassroomModePolicy(
            allowed_modes={"reflect", "quiz"}, forced_mode=None,
        )
        out = effective_mode(policy=policy, student_preference=None)
        assert out in {"reflect", "quiz"}


# ---------------------------------------------------------------------------
# Serialization — policy lives on the coordinator side, JSON-friendly
# ---------------------------------------------------------------------------


class TestPolicyRoundtrip:
    def test_to_dict_from_dict_roundtrip(self):
        original = ClassroomModePolicy(
            allowed_modes={"ask", "tutor", "quiz"},
            forced_mode="quiz",
        )
        restored = ClassroomModePolicy.from_dict(original.to_dict())
        assert restored.allowed_modes == original.allowed_modes
        assert restored.forced_mode == original.forced_mode

    def test_from_dict_tolerates_missing_fields(self):
        # Old manifests may lack these fields; default policy should apply.
        restored = ClassroomModePolicy.from_dict({})
        assert restored.forced_mode is None
        assert restored.allowed_modes  # non-empty
