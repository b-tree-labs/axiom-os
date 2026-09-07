# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Wire agent skills into the PromptComposer.

This is the runtime loop-closer between :mod:`axiom.agents.composable_skills`
(which knows how to compose a base SKILLS.md with extension-contributed
fragments) and :class:`axiom.infra.prompt_composer.PromptComposer` (the
seven-layer system-prompt builder that every agent invokes).

Until this module existed, the composer function was defined but nothing
invoked it against a running composer — the skill content never reached
the model. Agent loops (AXI, CHALKE, etc.) now call
:func:`weave_agent_skills` inside their ``_build_system_prompt`` method to
land the base persona + any fragments into the identity layer.

Robustness: a broken SKILLS.md, missing fragment file, or malformed
manifest never raises. Failures are logged at debug level and the prompt
build continues — matches the contract in
:mod:`axiom.infra.prompt_contributions`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from axiom.agents.composable_skills import (
    compose_agent_skills,
    discover_skill_fragments,
)

if TYPE_CHECKING:
    from axiom.infra.prompt_composer import PromptComposer

log = logging.getLogger(__name__)


def weave_agent_skills(
    composer: PromptComposer,
    *,
    agent_name: str,
    base_skills_path: Path | None,
    extension_dirs: Iterable[Path] = (),
    layer: str = "identity",
    required: bool = False,
) -> int:
    """Compose an agent's skills and add them to ``composer``.

    Reads ``base_skills_path`` (typically the agent's ``SKILLS.md`` or
    per-AEOS ``persona.md``), discovers any extension-contributed
    fragments declared via ``[[agent_skills.<agent_name>]]`` in each
    manifest under ``extension_dirs``, composes them into one markdown
    block, and writes it to the composer's ``layer`` (default:
    ``identity``).

    Returns the number of composer contributions added (0 or 1). The
    function is intentionally no-op-safe: no base file, no fragments, or
    any read/parse error yields 0 without raising.
    """
    base_content: str | None = None
    if base_skills_path is not None:
        try:
            base_content = Path(base_skills_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            log.debug(
                "weave_agent_skills[%s]: base skills file not found at %s",
                agent_name, base_skills_path,
            )
        except OSError as exc:
            log.debug(
                "weave_agent_skills[%s]: could not read %s (%s)",
                agent_name, base_skills_path, exc,
            )

    # Discover fragments from each extension dir. The underlying function
    # opens each manifest; wrap in a try so one broken manifest can't
    # starve the others.
    fragments: list[dict] = []
    for ext_dir in extension_dirs:
        try:
            fragments.extend(
                discover_skill_fragments(agent_name, [Path(ext_dir)])
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "weave_agent_skills[%s]: failed to read fragments from %s (%s)",
                agent_name, ext_dir, exc,
            )

    if base_content is None and not fragments:
        return 0

    composed = compose_agent_skills(base_content or "", fragments)
    if not composed.strip():
        return 0

    composer.add(
        layer,
        name=f"{agent_name}_skills",
        content=composed,
        source="agent_skills_runtime",
        required=required,
    )
    return 1


__all__ = ["weave_agent_skills"]
