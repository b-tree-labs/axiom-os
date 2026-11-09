# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Local, per-fragment system-prompt overrides.

Two prompt defects shipped today. The base preamble described a product that no
longer existed, and an extension asserted a closed tool list that made every
other extension's tools invisible to the model. Both were one sentence, and
neither could be tried differently without editing installed source.

An override replaces one named contribution and leaves the rest of the cascade
alone, so trying an idea does not mean forking the whole prompt. Setting one to
an empty string removes that fragment, which is how you find out whether a
piece is carrying its weight.

Precedence: a local override is the most specific thing in the cascade —
platform default, then shipped example, then extension contribution, then site
config, then this — so it wins over everything, including an extension that
believes it speaks for the whole system.

Nothing here raises. A prompt that will not build is a chat that will not
start, which is a worse failure than an experiment being ignored.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_FILENAME = "prompt-overrides.json"


def _path(state_dir: Path | None = None) -> Path:
    if state_dir is None:
        from axiom.infra.paths import get_project_state_dir

        state_dir = get_project_state_dir()
    return Path(state_dir) / _FILENAME


def load_overrides(*, state_dir: Path | None = None) -> dict[str, str]:
    """Every override currently set, or an empty mapping."""
    try:
        path = _path(state_dir)
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in (data or {}).items() if isinstance(v, str)}
    except Exception:  # noqa: BLE001 - corrupt file must not break the prompt
        _log.debug("prompt overrides unreadable; ignoring", exc_info=True)
        return {}


def list_overrides(*, state_dir: Path | None = None) -> dict[str, str]:
    """Alias for :func:`load_overrides`, for a caller that is reporting."""
    return load_overrides(state_dir=state_dir)


def set_override(name: str, content: str, *, state_dir: Path | None = None) -> None:
    """Override the contribution called ``name``. "" removes it entirely."""
    try:
        path = _path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        current = load_overrides(state_dir=state_dir)
        current[name] = content
        path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        _log.warning("could not save prompt override %r", name, exc_info=True)


def clear_override(name: str, *, state_dir: Path | None = None) -> None:
    """Restore ``name`` to whatever the cascade would otherwise produce."""
    try:
        current = load_overrides(state_dir=state_dir)
        if name not in current:
            return
        del current[name]
        _path(state_dir).write_text(json.dumps(current, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        _log.warning("could not clear prompt override %r", name, exc_info=True)


def apply_overrides(composer: Any, overrides: dict[str, str]) -> None:
    """Apply ``overrides`` to a built composer, in place.

    Only names that already exist are touched. An override for a fragment that
    is not there is ignored rather than added: a typo must not become a new,
    unattributed voice in the prompt.
    """
    if not overrides:
        return
    try:
        existing = {c.name: c for c in composer.debug()}
    except Exception:  # noqa: BLE001 - never break prompt building
        return
    for name, content in overrides.items():
        contribution = existing.get(name)
        if contribution is None:
            continue
        if content == "":
            composer.remove(contribution.layer, name)
            continue
        composer.add(
            contribution.layer,
            name=name,
            content=content,
            source="local-override",
            required=contribution.required,
        )


__all__ = [
    "apply_overrides",
    "clear_override",
    "list_overrides",
    "load_overrides",
    "set_override",
]
