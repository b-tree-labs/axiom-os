# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``identity`` — the operator surface for the local principal (IDENT-7).

Thin CLI noun (`axi identity init|whoami|status`) over skill functions, per
ADR-056. The identity *primitives* live in ``vega.identity`` (keypair, custody)
and ``infra.principal`` (posture, resolution); this extension just surfaces them.
"""

from axiom.extensions.builtins.identity.skills import init, status, whoami

__all__ = ["init", "status", "whoami"]
