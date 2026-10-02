# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""User-authored system-prompt fragments.

PromptComposer has seven canonical layers; until now, only extension
code added contributions. This loader gives users a file-based surface
for adding their own — drop a ``.md`` into:

  - ``$AXI_STATE_DIR/prompts/*.md`` (user scope)
  - ``<project_root>/.<cli_name>/prompts/*.md`` (project scope; wins on collision)

Each file becomes a contribution to a layer. Default layer is
``domain_context``. The file's stem is the contribution name; an
optional YAML frontmatter block carries::

    ---
    layer: policies          # one of LAYERS; fallback domain_context
    description: ...         # human-readable; not injected
    ---
    Body becomes the prompt text.

This is the parity equivalent of Cursor's ``.cursorrules`` and Claude
Code's ``CLAUDE.md`` — but layered into the existing PromptComposer
rather than monolithic, so users can target specific layers (policies,
identity, capabilities, ...) rather than only "extra system text."
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from axiom.infra.prompt_composer import LAYERS

log = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_DEFAULT_LAYER = "domain_context"


@dataclass(frozen=True)
class UserPrompt:
    name: str
    body: str
    layer: str
    description: str
    source: Path
    scope: Literal["user", "project"]


def _user_prompts_dir() -> Path | None:
    try:
        from axiom.infra.paths import get_user_state_dir

        return get_user_state_dir() / "prompts"
    except Exception:
        return None


def _project_prompts_dir() -> Path | None:
    try:
        from axiom.infra.branding import get_branding
        from axiom.infra.paths import get_project_root

        return get_project_root() / f".{get_branding().cli_name}" / "prompts"
    except Exception:
        return None


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw_meta, body = match.group(1), match.group(2)
    meta: dict[str, str] = {}
    for line in raw_meta.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key in {"layer", "description"}:
            meta[key] = value
    return meta, body.lstrip("\n")


def _load_dir(directory: Path | None, scope: Literal["user", "project"]) -> list[UserPrompt]:
    if directory is None or not directory.is_dir():
        return []
    out: list[UserPrompt] = []
    for path in sorted(directory.glob("*.md")):
        try:
            text = path.read_text()
        except OSError as exc:
            log.warning("user_prompts: cannot read %s: %s", path, exc)
            continue
        meta, body = _parse_frontmatter(text)
        layer = meta.get("layer", _DEFAULT_LAYER)
        if layer not in LAYERS:
            log.warning(
                "user_prompts: unknown layer %r in %s, falling back to %s",
                layer, path, _DEFAULT_LAYER,
            )
            layer = _DEFAULT_LAYER
        out.append(
            UserPrompt(
                name=path.stem,
                body=body,
                layer=layer,
                description=meta.get("description", ""),
                source=path,
                scope=scope,
            )
        )
    return out


def load_user_prompts() -> list[UserPrompt]:
    """Return all user + project prompts, project winning on name collision."""
    user_loaded = _load_dir(_user_prompts_dir(), "user")
    project_loaded = _load_dir(_project_prompts_dir(), "project")
    project_names = {p.name for p in project_loaded}
    return [p for p in user_loaded if p.name not in project_names] + project_loaded


def add_user_prompts_to(composer) -> int:
    """Add all loaded user prompts to ``composer``. Returns the count added."""
    prompts = load_user_prompts()
    for p in prompts:
        if not p.body.strip():
            continue
        try:
            composer.add(
                p.layer,
                name=f"user_prompt:{p.scope}:{p.name}",
                content=p.body,
                source=f"user:{p.source}",
                required=False,
            )
        except Exception as exc:
            log.warning("user_prompts: failed to add %s: %s", p.name, exc)
    return len(prompts)


__all__ = [
    "UserPrompt",
    "add_user_prompts_to",
    "load_user_prompts",
]
