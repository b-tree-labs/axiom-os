# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the routing-intent admission policy.

The subject of these tests is a decision, not a hint. A deployment declares
which knowledge sources may ground which kind of question, and what to do
when one of those sources is not answering. Everything here exists so that
"decline because nothing admissible is available" is something the platform
computes, rather than something a model is trusted to choose well.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from axiom.policy.routing_intent import (
    AdmissionStanding,
    DuplicateIntentError,
    IntentDeclarationError,
    IntentRegistry,
    UnavailablePolicy,
    UnknownIntentError,
    declare_from_mapping,
    load_intents,
)


def registry_with(*declarations: dict) -> IntentRegistry:
    """A registry holding the given raw declarations."""
    registry = IntentRegistry()
    for raw in declarations:
        declare_from_mapping(registry, raw)
    return registry


MEASURED = {
    "name": "measured",
    "sources": ["dp:gold_tier"],
    "determinism_class": "measurement",
    "unavailable_policy": "refuse",
}
NARRATIVE = {
    "name": "narrative",
    "sources": ["dp:rag_corpus", "dp:ops_log"],
    "determinism_class": "descriptive",
    "unavailable_policy": "degrade",
}


class TestDeclaration:
    """What a deployment must say, and what it may not leave unsaid."""

    def test_a_declared_intent_resolves_to_its_policy(self) -> None:
        registry = registry_with(MEASURED)
        policy = registry.resolve("measured")
        assert policy.sources == ("dp:gold_tier",)
        assert policy.unavailable_policy is UnavailablePolicy.REFUSE
        assert policy.determinism_class == "measurement"

    def test_an_undeclared_intent_fails_naming_what_is_declared(self) -> None:
        registry = registry_with(MEASURED, NARRATIVE)
        with pytest.raises(UnknownIntentError) as excinfo:
            registry.resolve("procedural")
        message = str(excinfo.value)
        assert "procedural" in message
        assert "measured" in message and "narrative" in message

    def test_there_is_no_fallback_intent(self) -> None:
        # An empty registry must not quietly admit everything. A deployment
        # that declares no intents has not opted out of the policy; it has
        # not finished configuring.
        registry = IntentRegistry()
        with pytest.raises(UnknownIntentError):
            registry.resolve("measured")

    def test_a_missing_unavailable_policy_is_refused_at_declaration(self) -> None:
        # Neither default is safe. Defaulting to degrade silently widens what
        # may ground an answer; defaulting to refuse silently breaks a
        # deployment that meant to keep answering. So the deployment says.
        raw = {k: v for k, v in MEASURED.items() if k != "unavailable_policy"}
        with pytest.raises(IntentDeclarationError) as excinfo:
            declare_from_mapping(IntentRegistry(), raw)
        assert "unavailable_policy" in str(excinfo.value)

    def test_an_unrecognised_unavailable_policy_names_the_choices(self) -> None:
        raw = dict(MEASURED, unavailable_policy="warn")
        with pytest.raises(IntentDeclarationError) as excinfo:
            declare_from_mapping(IntentRegistry(), raw)
        message = str(excinfo.value)
        assert "warn" in message
        assert "refuse" in message and "degrade" in message

    def test_an_intent_with_no_sources_is_refused_at_declaration(self) -> None:
        # An intent that admits nothing can only ever produce an ungrounded
        # answer, so it is a configuration mistake rather than a strict setting.
        raw = dict(MEASURED, sources=[])
        with pytest.raises(IntentDeclarationError) as excinfo:
            declare_from_mapping(IntentRegistry(), raw)
        assert "sources" in str(excinfo.value)

    def test_declaring_the_same_intent_twice_is_an_error(self) -> None:
        registry = registry_with(MEASURED)
        with pytest.raises(DuplicateIntentError):
            declare_from_mapping(registry, dict(MEASURED, sources=["dp:anything"]))

    def test_declared_returns_the_names_in_sorted_order(self) -> None:
        registry = registry_with(NARRATIVE, MEASURED)
        assert registry.declared() == ("measured", "narrative")


class TestAdmission:
    """The decision itself, computed before anything is generated."""

    def test_every_source_available_is_admitted(self) -> None:
        registry = registry_with(MEASURED)
        admission = registry.admit("measured", available={"dp:gold_tier"})
        assert admission.standing is AdmissionStanding.ADMITTED
        assert admission.admissible == ("dp:gold_tier",)
        assert admission.missing == ()

    def test_refuse_policy_refuses_when_a_source_is_missing(self) -> None:
        registry = registry_with(MEASURED)
        admission = registry.admit("measured", available=set())
        assert admission.standing is AdmissionStanding.REFUSED
        assert admission.missing == ("dp:gold_tier",)

    def test_degrade_policy_answers_from_what_is_left(self) -> None:
        registry = registry_with(NARRATIVE)
        admission = registry.admit("narrative", available={"dp:rag_corpus"})
        assert admission.standing is AdmissionStanding.DEGRADED
        assert admission.admissible == ("dp:rag_corpus",)
        assert admission.missing == ("dp:ops_log",)

    def test_refuse_policy_refuses_even_with_sources_left(self) -> None:
        # The branch that makes `refuse` mean anything. With a single-source
        # intent, losing that source empties the admissible set and the
        # no-source rule refuses on its own, so `refuse` and `degrade` would
        # be indistinguishable. Two sources with one still answering is the
        # only shape that tells them apart: degrade would answer from what is
        # left, and refuse must not.
        strict_pair = dict(MEASURED, name="strict_pair", sources=["dp:a", "dp:b"])
        registry = registry_with(strict_pair)
        admission = registry.admit("strict_pair", available={"dp:a"})
        assert admission.standing is AdmissionStanding.REFUSED
        assert admission.admissible == ("dp:a",), (
            "a refusal still reports what was available, so an operator can "
            "see the source set was incomplete rather than empty"
        )
        assert admission.missing == ("dp:b",)

    def test_the_two_policies_differ_on_exactly_this_case(self) -> None:
        # Pins the pair against each other, so a change that collapses one
        # into the other fails here regardless of which direction it went.
        registry = registry_with(
            dict(MEASURED, name="strict", sources=["dp:a", "dp:b"]),
            dict(NARRATIVE, name="lenient", sources=["dp:a", "dp:b"]),
        )
        strict = registry.admit("strict", available={"dp:a"})
        lenient = registry.admit("lenient", available={"dp:a"})
        assert strict.standing is AdmissionStanding.REFUSED
        assert lenient.standing is AdmissionStanding.DEGRADED
        assert strict.standing is not lenient.standing

    def test_degrade_with_nothing_left_still_refuses(self) -> None:
        # The sharp case. "Degrade" means answer from less, and with nothing
        # admissible there is no less to answer from — only an ungrounded
        # answer wearing a policy's approval. A deployment that wants a
        # source-free answer has to say so by declaring an intent that
        # admits that source, not by losing every source it named.
        registry = registry_with(NARRATIVE)
        admission = registry.admit("narrative", available=set())
        assert admission.standing is AdmissionStanding.REFUSED
        assert admission.admissible == ()
        assert "no admissible source" in admission.reason

    def test_an_undeclared_source_being_up_does_not_help(self) -> None:
        # Availability is intersected with the declaration, never unioned.
        # A source nobody admitted for this intent cannot rescue it.
        registry = registry_with(MEASURED)
        admission = registry.admit("measured", available={"dp:rag_corpus"})
        assert admission.standing is AdmissionStanding.REFUSED
        assert admission.admissible == ()

    def test_admitting_an_undeclared_intent_fails_loudly(self) -> None:
        registry = registry_with(MEASURED)
        with pytest.raises(UnknownIntentError):
            registry.admit("procedural", available={"dp:gold_tier"})

    def test_the_admission_carries_the_intent_and_its_determinism_class(self) -> None:
        registry = registry_with(MEASURED)
        admission = registry.admit("measured", available={"dp:gold_tier"})
        assert admission.intent == "measured"
        assert admission.determinism_class == "measurement"


class TestAdmissionIsNotABoolean:
    """Three standings do not collapse into two.

    ``if admission:`` is the bug this prevents. Whatever truthiness would
    mean, one of the three standings reads wrong under it: DEGRADED is not a
    clean pass, and REFUSED is not merely a falsy nothing — it is a decision
    with a reason that a caller owes the user.
    """

    @pytest.mark.parametrize("available", [{"dp:gold_tier"}, set()], ids=["admitted", "refused"])
    def test_truth_testing_an_admission_raises(self, available) -> None:
        registry = registry_with(MEASURED)
        admission = registry.admit("measured", available=available)
        with pytest.raises(TypeError) as excinfo:
            bool(admission)
        assert "standing" in str(excinfo.value)

    def test_a_degraded_admission_also_refuses_truth_testing(self) -> None:
        registry = registry_with(NARRATIVE)
        admission = registry.admit("narrative", available={"dp:rag_corpus"})
        with pytest.raises(TypeError):
            bool(admission)

    def test_an_admission_is_frozen(self) -> None:
        registry = registry_with(MEASURED)
        admission = registry.admit("measured", available={"dp:gold_tier"})
        with pytest.raises(FrozenInstanceError):
            admission.standing = AdmissionStanding.ADMITTED  # type: ignore[misc]


class TestTheSchemaNamesNoDomain:
    """Axiom declares the shape; a deployment supplies the vocabulary."""

    def test_no_intent_name_or_source_id_is_baked_into_the_module(self) -> None:
        from pathlib import Path

        import axiom.policy.routing_intent as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        for word in ("measured", "narrative", "gold_tier", "rag_corpus"):
            assert word not in source, (
                f"{word!r} is a deployment's vocabulary, not the platform's. "
                "Intent names and source ids are values a declaration carries."
            )


class TestExplaining:
    """A refusal a person can act on."""

    def test_a_refusal_explains_which_sources_were_missing(self) -> None:
        registry = registry_with(NARRATIVE)
        admission = registry.admit("narrative", available={"dp:rag_corpus"})
        assert "dp:ops_log" in admission.reason

    def test_an_admitted_decision_still_carries_a_reason(self) -> None:
        registry = registry_with(MEASURED)
        admission = registry.admit("measured", available={"dp:gold_tier"})
        assert admission.reason


class TestLoadingADeploymentsBlock:
    """The shape a deployment actually writes, loaded in one call."""

    # The vocabulary below is a deployment's, supplied here as test data. The
    # module itself must not contain any of it; TestTheSchemaNamesNoDomain
    # enforces that.
    BLOCK = {
        "intents": {
            "measured": {
                "sources": ["dp:gold_tier"],
                "determinism_class": "measurement",
                "unavailable_policy": "refuse",
            },
            "narrative": {
                "sources": ["dp:rag_corpus"],
                "determinism_class": "descriptive",
                "unavailable_policy": "degrade",
            },
        }
    }

    def test_the_declared_shape_loads(self) -> None:
        registry = load_intents(self.BLOCK)
        assert registry.declared() == ("measured", "narrative")
        assert registry.resolve("measured").unavailable_policy is UnavailablePolicy.REFUSE
        assert registry.resolve("narrative").unavailable_policy is UnavailablePolicy.DEGRADE

    def test_the_key_becomes_the_intent_name(self) -> None:
        registry = load_intents(self.BLOCK)
        assert registry.resolve("measured").name == "measured"

    def test_one_malformed_intent_fails_the_whole_load(self) -> None:
        # Partial success would leave a registry quietly missing an intent
        # somebody believes they declared, which fails later and elsewhere.
        block = {
            "intents": {
                "good": {
                    "sources": ["dp:a"],
                    "determinism_class": "x",
                    "unavailable_policy": "refuse",
                },
                "bad": {"sources": ["dp:b"], "determinism_class": "x"},
            }
        }
        with pytest.raises(IntentDeclarationError) as excinfo:
            load_intents(block)
        assert "unavailable_policy" in str(excinfo.value)

    def test_a_missing_intents_key_is_an_error_not_an_empty_registry(self) -> None:
        with pytest.raises(IntentDeclarationError) as excinfo:
            load_intents({"something_else": {}})
        assert "intents" in str(excinfo.value)

    def test_an_explicitly_empty_intents_mapping_is_allowed(self) -> None:
        registry = load_intents({"intents": {}})
        assert registry.declared() == ()
        with pytest.raises(UnknownIntentError):
            registry.resolve("anything")
