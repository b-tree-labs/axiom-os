# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ProvenanceRef — pointer to the fragment that caused an action.

Per spec-governance-fabric §1.2: every `ActionEnvelope` carries a
`provenance_parent` field. The reserved value ``ProvenanceRef.SYNTHETIC``
is for boot-time / synthetic actions only (lint catches abuse).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProvenanceRef:
    """A reference to the fragment that motivated an action.

    Typically a memory-fragment URI: ``memory://<authority>/fragments/<id>``.
    The reserved sentinel ``SYNTHETIC`` is for the three legitimate
    boot-time / synthetic-action construction sites only.
    """

    fragment_uri: str

    @classmethod
    def synthetic(cls, reason: str) -> ProvenanceRef:
        """Reserved for synthetic boot-time actions.

        The lint allowlist for synthetic actions checks for this construction
        path. ``reason`` is captured in the receipt for audit.
        """
        return cls(fragment_uri=f"synthetic://boot/{reason}")

    @property
    def is_synthetic(self) -> bool:
        return self.fragment_uri.startswith("synthetic://")

    def __str__(self) -> str:
        return self.fragment_uri


# A module-level constant for the common no-parent case.
SYNTHETIC = ProvenanceRef.synthetic("unattributed")


__all__ = ["ProvenanceRef", "SYNTHETIC"]
