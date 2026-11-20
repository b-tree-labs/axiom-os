# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for the RAG-eval generator's telemetry guard.

The generator must not emit single-hop questions that are instantaneous
time-series lookups (a value AT a specific time/date). Those belong in the
structured/time-series tier, not document-RAG; generating them and scoring
RAG's correct refusal as a miss is the eval-design pollution that depressed
the robust grounding absolute. This locks the classifier so the filter can't
silently regress (no live gateway/DB needed).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load the script as a module (scripts/ isn't a package).
_SCRIPT = Path(__file__).parent.parent / "scripts" / "gen_rag_eval.py"
_spec = importlib.util.spec_from_file_location("gen_rag_eval", _SCRIPT)
assert _spec and _spec.loader
gen_rag_eval = importlib.util.module_from_spec(_spec)
sys.modules["gen_rag_eval"] = gen_rag_eval
_spec.loader.exec_module(gen_rag_eval)

_is_telemetry = gen_rag_eval._is_telemetry_question


# -- telemetry questions that MUST be dropped --------------------------------

TELEMETRY_CASES = [
    ("What was the maximum reactor power on 9 August 2004?", "0.602 MW"),
    ("What was the reactor power on August 9, 2004?", "0.602 MW"),
    ("What temperature was recorded at 14:32?", "48 C"),
    ("What was the coolant flow on 2004-08-09?", "120 gpm"),
    ("What power level was logged that day?", "0.6 MW"),
    ("What was the instantaneous reading on the console?", "250 kW"),
    ("What pressure was measured at the time of the trip?", "30 psig"),
]


# -- legitimate document facts that MUST survive -----------------------------

DOCUMENT_CASES = [
    # Rated/design spec — a units answer with NO time/date and NO snapshot word.
    ("What is the rated thermal power of the TRIGA reactor?", "1.1 MW"),
    ("What fuel material does the core use?", "uranium zirconium hydride"),
    ("What moderator is used in the reactor design?", "light water"),
    ("What is the cladding material of the fuel elements?", "stainless steel"),
    # A date appears, but as a design/event fact, not a value-at-time lookup.
    ("What enrichment is specified for the fuel?", "19.7% U-235"),
]


def test_telemetry_questions_are_filtered():
    for q, a in TELEMETRY_CASES:
        assert _is_telemetry(q, a) is True, f"should be filtered: {q!r} -> {a!r}"


def test_document_facts_survive():
    for q, a in DOCUMENT_CASES:
        assert _is_telemetry(q, a) is False, f"should survive: {q!r} -> {a!r}"


def test_units_answer_without_snapshot_phrasing_survives():
    # A bare units answer alone is a design spec, not telemetry — keep it.
    assert _is_telemetry("What is the maximum licensed power?", "1.1 MW") is False


def test_units_answer_with_snapshot_phrasing_is_telemetry():
    # Same units answer, but snapshot phrasing makes it a reading.
    assert _is_telemetry("What power was recorded?", "1.1 MW") is True


def test_empty_inputs_are_safe():
    assert _is_telemetry("", "") is False
