# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Optional extensions: installed with the platform, off until a node turns them on.

An extension declares itself optional in its manifest::

    [extension.optional]
    default = "off"                 # "off" (the point of the block) or "on"
    in_use_when = ["<name>"]        # state-dir paths whose presence means this
                                    # node already uses it

and is then **enabled on a node** when, in order:

1. the node's ``features`` file switches it off by name (``off = [...]``): off;
2. it lists the name under ``features``: on;
3. one of its ``in_use_when`` paths exists under the node's state directory:
   on, so a node that was already using the extension is never silently
   switched off by an upgrade;
4. otherwise its ``default``.

A disabled optional extension is still installed and importable; it simply
contributes nothing: discovery marks it ``enabled = False``, so its commands,
MCP tools, skills, HTTP mounts, background agent and declared schedules are
left out. It is switched with ``features enable <name>`` / ``features disable
<name>``, needing no reinstall. Nothing here names any extension: everything
an optional extension needs is in its own manifest (contribute by declaration).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def optional_block(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``[extension.optional]`` table of a parsed manifest, or ``None``."""
    ext = manifest.get("extension") if isinstance(manifest, Mapping) else None
    block = ext.get("optional") if isinstance(ext, Mapping) else None
    return dict(block) if isinstance(block, Mapping) else None


def _state_dir() -> Path | None:
    try:
        from axiom.infra.paths import get_user_state_dir

        return Path(get_user_state_dir())
    except Exception:  # noqa: BLE001 - no state dir means no evidence of use
        return None


def in_use(block: Mapping[str, Any], state_dir: Path | None = None) -> bool:
    """Does this node already use the extension (an ``in_use_when`` path exists)?"""
    paths = block.get("in_use_when")
    if not isinstance(paths, list) or not paths:
        return False
    root = state_dir if state_dir is not None else _state_dir()
    if root is None:
        return False
    for rel in paths:
        if isinstance(rel, str) and rel and not Path(rel).is_absolute() and ".." not in Path(rel).parts:
            if (root / rel).exists():
                return True
    return False


def is_enabled(
    name: str,
    block: Mapping[str, Any] | None,
    *,
    cfg: Any = None,
    state_dir: Path | None = None,
) -> bool:
    """Whether the extension ``name`` with this ``[extension.optional]`` block is on here.

    A non-optional extension (``block is None``) is always on. A node file that
    cannot be read reads as undecided, so the evidence and the default decide.
    """
    if block is None:
        return True
    if cfg is None:
        try:
            from axiom.infra import node_functions

            cfg = node_functions.load()
        except Exception:  # noqa: BLE001
            cfg = None
    off = tuple(getattr(cfg, "off", ()) or ())
    features = tuple(getattr(cfg, "features", ()) or ())
    if name in off:
        return False
    if name in features:
        return True
    if in_use(block, state_dir):
        return True
    return str(block.get("default", "off")).lower() == "on"


def manifest_enabled(manifest: Mapping[str, Any], *, cfg: Any = None, state_dir: Path | None = None) -> bool:
    """:func:`is_enabled` for a raw parsed manifest dict."""
    ext = manifest.get("extension") if isinstance(manifest, Mapping) else None
    name = str(ext.get("name") or "") if isinstance(ext, Mapping) else ""
    return is_enabled(name, optional_block(manifest), cfg=cfg, state_dir=state_dir)


_blocks: dict[str, dict[str, Any]] | None = None


def optional_blocks() -> dict[str, dict[str, Any]]:
    """Every discovered optional extension's ``[extension.optional]`` block, by name.

    A function of the extension tree alone, so it is discovered once per
    process; the node's switch is applied on every read (:func:`switched_off`,
    :func:`optional_extensions`), so turning an extension on or off needs no
    rediscovery.
    """
    global _blocks
    if _blocks is None:
        from axiom.extensions.discovery import discover_extensions

        found: dict[str, dict[str, Any]] = {}
        for ext in discover_extensions():
            block = getattr(ext, "optional", None)
            if isinstance(block, dict):
                # The manifest's description rides along for the switchboard's row.
                found[ext.name] = {**block, "description": str(getattr(ext, "description", "") or "")}
        _blocks = found
    return dict(_blocks)


def set_optional_blocks(blocks: Mapping[str, Mapping[str, Any]] | None) -> None:
    """Pin the optional blocks (tests), or ``None`` to rediscover on next use."""
    global _blocks
    _blocks = None if blocks is None else {k: dict(v) for k, v in blocks.items()}


def optional_extensions(*, cfg: Any = None, state_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Every discovered optional extension: name -> its block, plus ``enabled`` here and now."""
    if cfg is None:
        cfg = _node_cfg()
    return {
        name: {**block, "enabled": is_enabled(name, block, cfg=cfg, state_dir=state_dir)}
        for name, block in optional_blocks().items()
    }


def switched_off(*, cfg: Any = None, state_dir: Path | None = None) -> frozenset[str]:
    """The optional extensions that are off on this node, read fresh from the node file.

    This is the set the switchboard's resolution rule reads (ADR-254): an
    extension that is off makes every function and flag it owns resolve off,
    on every surface.
    """
    if cfg is None:
        cfg = _node_cfg()
    return frozenset(
        name for name, block in optional_blocks().items()
        if not is_enabled(name, block, cfg=cfg, state_dir=state_dir)
    )


def _node_cfg() -> Any:
    try:
        from axiom.infra import node_functions

        return node_functions.load()
    except Exception:  # noqa: BLE001 - an unreadable node file reads as undecided
        return None


__all__ = [
    "in_use",
    "is_enabled",
    "manifest_enabled",
    "optional_block",
    "optional_blocks",
    "optional_extensions",
    "set_optional_blocks",
    "switched_off",
]
