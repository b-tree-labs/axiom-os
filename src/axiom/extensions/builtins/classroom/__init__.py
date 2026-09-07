# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom classroom extension — federated cohort + materials + learning modes.

Hosts the CHALKE AI Training Assistant agent (see
``agents/chalke/persona.md``) and the full classroom CLI surface:

- Cohort ceremony: ``invite``, ``serve``, ``join``
- Materials: signed manifest + content-addressed download
- Learning modes: ask / tutor / quiz / reflect / review
- Briefs: per-student summaries with instructor curation
- Threads: bidirectional instructor ↔ student question threads
- Quizzes: instructor-broadcast, closed-book retrieval practice
- Evals: question-bank scoring with optional baseline comparison

Per ADR-031, this extension is destined for extraction to its own
repo (Keplo). The package keeps imports contained to make that
extraction clean: classroom code depends on ``axiom.vega.federation``,
``axiom.vega.identity``, ``axiom.graph``, ``axiom.infra.gateway``, and the
``http`` built-in extension — those will become external
dependencies of the eventual Keplo package.
"""
