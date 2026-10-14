# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The product a node's web surfaces present themselves as.

A node serves several web surfaces — the sign-in gate, the application shell,
whatever else composes in — and to the person using it they are one product.
Each surface resolving its own brand is how they came to disagree: the
application mount read ``AXIOM_BRAND_NAME`` from the environment, the gate
router took a constructor argument that no caller passed, and so a node
started with the environment set introduced itself correctly on every page
EXCEPT the first one anybody saw.

This is the one resolver. A surface that needs a brand calls it; a surface
that invents its own is reintroducing the defect.

Distinct from :mod:`axiom.infra.branding`, which is the CLI identity registry
a consumer's entry point installs in-process before handing off to Axiom's
CLI. A web server is started by a unit file rather than by that entry point,
so its brand arrives through the environment instead. Same intent, different
door; keeping them separate is what stops a web default from depending on
whether some CLI ran first.

Per ADR-048 this white-labels IDENTITY only. The borrowed platform machinery
underneath keeps its neutral name, the way ``systemd`` is called ``systemd``
on every distribution that ships it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

#: Axiom's own accent (UT burnt orange), and the fallback for every surface.
DEFAULT_ACCENT = "#bf5700"

#: Axiom's own name. A deploy that sets nothing is Axiom, not a blank.
DEFAULT_PRODUCT_NAME = "Axiom"

#: The environment contract. Named here so a surface never spells them itself.
PRODUCT_NAME_VAR = "AXIOM_BRAND_NAME"
ACCENT_VAR = "AXIOM_BRAND_ACCENT"


@dataclass(frozen=True)
class WebBrand:
    """What every web surface on this node calls itself and paints with."""

    product_name: str = DEFAULT_PRODUCT_NAME
    accent: str = DEFAULT_ACCENT

    def payload(self) -> dict:
        """The shape a surface injects for its bundle to read at boot."""
        return {"product_name": self.product_name, "accent": self.accent}


def _set(env: Mapping[str, str], key: str, fallback: str) -> str:
    """A variable's value, or the fallback when it is absent or blank.

    Blank matters: ``AXIOM_BRAND_NAME=`` in a unit file is an operator
    leaving the knob alone, not declaring a nameless product. Taking "" as a
    name renders a heading with no text and an initial tile with no letter,
    which reads as a broken page rather than as the default.
    """
    value = env.get(key)
    return value.strip() if value and value.strip() else fallback


def resolve_web_brand(env: Mapping[str, str] | None = None) -> WebBrand:
    """This node's web brand, read fresh from the environment each call.

    Fresh, not frozen at import: a module-level constant would capture
    whatever was set when something first imported this file, which under a
    test run is a neighbouring test's environment and under uvicorn is
    whatever the process inherited before its unit file was applied.

    ``env`` is the injection seam. Production passes nothing.
    """
    source = os.environ if env is None else env
    return WebBrand(
        product_name=_set(source, PRODUCT_NAME_VAR, DEFAULT_PRODUCT_NAME),
        accent=_set(source, ACCENT_VAR, DEFAULT_ACCENT),
    )


__all__ = [
    "ACCENT_VAR",
    "DEFAULT_ACCENT",
    "DEFAULT_PRODUCT_NAME",
    "PRODUCT_NAME_VAR",
    "WebBrand",
    "resolve_web_brand",
]
