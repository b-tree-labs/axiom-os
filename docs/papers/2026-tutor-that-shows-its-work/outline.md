# The Tutor That Shows Its Work

## Provenance-Grounded AI Tutoring for STEM Outcomes

**Product:** KEP-LO (Keplo)
**Copyright holder:** The University of Texas at Austin (per ownership-map.md)
**Authors:** Benjamin Booth (UT Austin), Ondrej Chvala (UT Knoxville), Soha Ansari — co-authors TBD
**Status:** Placeholder — see project_paper_portfolio_titles memory
**Target venue:** AIED (AI in Education) / LAK (Learning Analytics) / NeurIPS ML4Ed workshop
**Date:** 2026-04-20

---

## One-line contribution

AI tutoring that produces measurably better STEM learning outcomes
than general-purpose LLM tutoring *and* traces every assessment score
to its supporting evidence chain — the first audit-defensible AI
tutor.

## Public-benchmark anchors

- **MMLU-STEM subset** — pre/post delta on established standard
- Course-final-exam scores (a STEM-course cohort at a partner institution)
- Optional: **GSM8K**-adjacent reasoning for math components
- *Novel:* interaction-quality metric from per-student knowledge graph

## Scale-inflection hook

Per-student intervention precision stays **linear** in Keplo
(knowledge graph per student, indexed). Opaque general-purpose
tutors lose context at O(N²) cost as cohorts grow because every
student competes for the same context window.

## Status

Outline not yet written. Gating: Prague cohort run (summer 2026) +
follow-on cohort for statistical power (likely Q1 2027). Paper
target: Q2 2027 submission. IRB protocol in progress per
`project_prague_deployment_requirements`.

Source material:

- `axiom/docs/prds/prd-classroom.md`
- `axiom/docs/specs/spec-classroom.md`
- `axiom/docs/working/classroom-user-journeys.md`
- `axiom/docs/working/prague-deployment-runbook.md`

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
