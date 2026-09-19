# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom identity: Ed25519 keypairs + Matrix-style principal handles.

Foundation for ADR-020 four identity layers (platform/node/affiliation/
context) and ADR-021 signed-content-addressed findings. Private keys
never leave the process that generated them; verification is a pure
function of public bytes + message + signature.
"""

from __future__ import annotations

from axiom.vega.identity.keypair import Keypair, generate_keypair, verify
from axiom.vega.identity.principal import Principal

__all__ = ["Keypair", "Principal", "generate_keypair", "verify"]
