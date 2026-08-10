# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``cred`` — the personal credential fabric (`axi cred`). Store/retrieve any
credential for any system, gated by the local principal's posture (ADR-074)."""

from axiom.extensions.builtins.cred.store import CredStore

__all__ = ["CredStore"]
