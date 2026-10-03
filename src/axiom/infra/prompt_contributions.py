# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Declarative PromptComposer contributions via extension manifest (#84).

Extensions declare which system-prompt layer they contribute to in
``axiom-extension.toml``:

    [[prompt_contributions]]
    layer = "domain_context"
    name = "classroom_role"
    source_module = "axiom.extensions.builtins.classroom.prompt_layers"
    source_function = "build_classroom_role_context"
    required = false

The named function takes one argument — an ``agent_context`` dict
supplied by the caller — and returns either:

    - ``str``  → the content to add to the layer
    - ``None`` → skip this contribution for this turn

All errors are swallowed: a broken contributor must never break the
agent's prompt build.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from axiom.infra.prompt_composer import LAYERS, PromptComposer

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptContributionDef:
    """One declared prompt-layer contribution from a manifest."""

    layer: str
    name: str
    source_module: str
    source_function: str
    required: bool = False


def _resolve(def_: PromptContributionDef) -> Callable[[dict[str, Any]], str | None] | None:
    """Import + fetch the contributor function. Returns None on failure."""
    try:
        module = importlib.import_module(def_.source_module)
    except Exception as exc:
        log.debug(
            "prompt_contribution %r: could not import %s (%s)",
            def_.name, def_.source_module, exc,
        )
        return None
    fn = getattr(module, def_.source_function, None)
    if not callable(fn):
        log.debug(
            "prompt_contribution %r: %s.%s is not callable",
            def_.name, def_.source_module, def_.source_function,
        )
        return None
    return fn  # type: ignore[return-value]


def apply_prompt_contributions(
    composer: PromptComposer,
    *,
    contributions: Iterable[PromptContributionDef],
    context: dict[str, Any],
    extension_name: str,
) -> None:
    """Invoke each contributor and add its output to the composer.

    Safe to call even when the composer already has imperatively-added
    contributions — names are scoped by extension to reduce collision
    risk (final name = ``{extension_name}:{pc.name}``).
    """
    for pc in contributions:
        if pc.layer not in LAYERS:
            log.warning(
                "extension %s: unknown prompt-composer layer %r — skipping %r",
                extension_name, pc.layer, pc.name,
            )
            continue
        fn = _resolve(pc)
        if fn is None:
            continue
        try:
            content = fn(context)
        except Exception as exc:
            log.warning(
                "extension %s: contribution %r raised %s — skipping",
                extension_name, pc.name, exc,
            )
            continue
        if not content:
            continue
        composer.add(
            pc.layer,
            name=f"{extension_name}:{pc.name}",
            content=str(content),
            source=f"{extension_name}/manifest",
            required=pc.required,
        )
