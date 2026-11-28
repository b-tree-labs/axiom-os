# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The command a person typed: the active distribution's program, then ``instrument``.

A refusal or a usage line names ``acme instrument ...`` under a product branded
``acme``, never a literal ``axi``.
"""

from __future__ import annotations


def cli_name() -> str:
    """The active brand's program name (``axi``, or a consumer's)."""
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001 - a label never takes a command down
        return "axi"


def noun_prog() -> str:
    """``<cli> instrument``: what every usage line and fix starts with."""
    return f"{cli_name()} instrument"


__all__ = ["cli_name", "noun_prog"]
