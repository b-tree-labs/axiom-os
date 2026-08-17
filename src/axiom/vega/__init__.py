# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Vega — federation, identity, and trust infrastructure.

Pre-extraction staging directory per ADR-031 Phase 3. Vega is UT-owned;
its eventual home is a separate repository (see
``docs/working/plan-vega-extraction.md``). Until then, Vega code lives
here, alongside Axiom, with import-linter rules preventing coupling
back into Axiom internals.

Subpackages:

* ``axiom.vega.federation`` — cohort registry, A2A protocol, agent
  cards, mDNS discovery, knowledge metrics, peer liveness.
* ``axiom.vega.identity`` — Ed25519 keypairs, principals, signing.

The legacy import paths ``axiom.federation`` and ``axiom.identity``
were removed in this refactor; all code now imports from the canonical
``axiom.vega.*`` paths above.
"""
