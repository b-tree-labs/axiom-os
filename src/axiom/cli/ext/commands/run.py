# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext run <ext>.<kind>.<name> [args...]`` — execute a declared capability.

v0.1 scope:

- ``kind == "cmd"`` — load the entry point declared in the manifest and
  invoke it with the remaining args. The entry string is the standard
  ``module.path:attr`` form (matching ``pyproject.toml`` entry points).
  If ``attr`` resolves to a plain callable we call ``attr(args)``.
- Any other kind — exit 2 with a clear message pointing at v0.2.

Why not delegate to each kind's own runner? v0.1 only needs the ``cmd``
path for migration smoke-tests. Stubbing the others keeps the surface
honest (``axi ext run foo.agent.bar`` is not silently broken) without
pulling in agent runtime machinery this extension doesn't need yet.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tomllib
from pathlib import Path
from typing import Any

from axiom.cli.ext._output import console
from axiom.cli.ext.provider import CliContext

# Kinds we support running in v0.1. Everything else stubs to exit-2.
_SUPPORTED_KINDS: frozenset[str] = frozenset({"cmd"})


def _parse_capability_spec(spec: str) -> tuple[str, str | None, str | None]:
    """Split ``<ext>[.<kind>.<name>]`` into its parts.

    Returns ``(ext, kind_or_None, name_or_None)``. The bare-``<ext>`` form
    is valid — callers then default to the extension's sole declared cmd,
    or emit an ambiguity error for multi-cmd / no-cmd extensions.

    The ``name`` segment may itself contain dots (e.g. a compound noun),
    so we consume the *first two* dot-separated components as ``ext``
    and ``kind`` and join the remainder back as ``name``.
    """
    if not spec or ".." in spec:
        raise ValueError(f"invalid capability spec: {spec!r}")
    parts = spec.split(".")
    if len(parts) == 1:
        if not parts[0]:
            raise ValueError(f"invalid capability spec: {spec!r}")
        return parts[0], None, None
    if len(parts) < 3:
        raise ValueError(
            f"invalid capability spec {spec!r}: expected <ext> or "
            "<ext>.<kind>.<name>"
        )
    ext, kind, *name_parts = parts
    if not ext or not kind or not name_parts:
        raise ValueError(f"invalid capability spec: {spec!r}")
    return ext, kind, ".".join(name_parts)


def _list_manifest_cmds(ext_path: Path) -> list[dict]:
    """Return every ``[[extension.provides]]`` block with ``kind = 'cmd'``."""
    manifest = ext_path / "axiom-extension.toml"
    if not manifest.exists():
        return []
    with manifest.open("rb") as fh:
        data = tomllib.load(fh)
    provides = data.get("extension", {}).get("provides", []) or []
    return [b for b in provides if b.get("kind") == "cmd"]


def _resolve_default_cmd(
    ext_path: Path,
) -> tuple[dict | None, list[dict]]:
    """Return ``(sole_cmd_block, all_cmd_blocks)`` — sole is None if != 1."""
    cmds = _list_manifest_cmds(ext_path)
    if len(cmds) == 1:
        return cmds[0], cmds
    return None, cmds


def _installed_extension_path(ext_name: str) -> Path | None:
    """Return the root directory for an installed extension, or None.

    Wraps :func:`axiom.extensions.discovery.discover_extensions` so tests
    can monkeypatch this single seam.
    """
    try:
        from axiom.extensions.discovery import discover_extensions
    except Exception:  # noqa: BLE001 — degrade gracefully
        return None
    try:
        for ext in discover_extensions():
            if getattr(ext, "name", None) == ext_name:
                root = getattr(ext, "root", None)
                if root is not None:
                    return Path(root)
    except Exception:  # noqa: BLE001
        return None
    return None


def _resolve_entry(ext_path: Path, *, kind: str, name: str) -> str | None:
    """Return the manifest ``entry`` string for the named capability or None."""
    manifest = ext_path / "axiom-extension.toml"
    if not manifest.exists():
        return None
    with manifest.open("rb") as fh:
        data = tomllib.load(fh)
    for block in data.get("extension", {}).get("provides", []) or []:
        if block.get("kind") != kind:
            continue
        # Different kinds use different identifying fields; check all.
        label = (
            block.get("name")
            or block.get("noun")
            or block.get("integration")
        )
        if label == name and "entry" in block:
            return str(block["entry"])
    return None


def _load_entry(entry: str) -> Any:
    """Load a ``module.path:attr`` entry string and return the attribute."""
    if ":" not in entry:
        raise ValueError(
            f"entry {entry!r} is not a valid module:attr reference"
        )
    module_path, attr = entry.split(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class RunProvider:
    """Built-in provider for ``axi ext run <ext>.<kind>.<name> [args...]``."""

    verb = "run"
    description = "Execute a declared capability (v0.1: kind=cmd only)"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "capability",
            help="Capability spec in the form <ext>.<kind>.<name>",
        )
        parser.add_argument(
            "cap_args",
            nargs=argparse.REMAINDER,
            help="Arguments forwarded to the capability's entry point",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        con = console()
        try:
            ext_name, kind, name = _parse_capability_spec(args.capability)
        except ValueError as exc:
            con.print(f"axi ext run: {exc}")
            return 2

        ext_path = _installed_extension_path(ext_name)
        if ext_path is None:
            con.print(
                f"axi ext run: extension {ext_name!r} is not installed. "
                "Run `axi ext list` to see installed extensions."
            )
            return 1

        # Bare ``axi ext run <ext>`` — default to the sole declared cmd when
        # exactly one exists; otherwise surface the ambiguity.
        if kind is None and name is None:
            sole, all_cmds = _resolve_default_cmd(ext_path)
            if sole is None:
                if not all_cmds:
                    con.print(
                        f"axi ext run: no runnable cmd declared on "
                        f"{ext_name!r}. Declare one with "
                        "`kind = \"cmd\"` in axiom-extension.toml."
                    )
                    return 1
                labels = ", ".join(
                    f"{ext_name}.cmd.{c.get('name') or c.get('noun') or ''}"
                    for c in all_cmds
                )
                con.print(
                    f"axi ext run: {ext_name!r} declares multiple cmds; "
                    f"pick one — {labels}"
                )
                return 1
            kind = "cmd"
            name = sole.get("name") or sole.get("noun") or ""
            if not name:
                con.print(
                    f"axi ext run: {ext_name!r} cmd block has no name/noun"
                )
                return 1

        if kind not in _SUPPORTED_KINDS:
            con.print(
                f"axi ext run: does not yet support kind={kind!r} "
                f"(capability {args.capability!r}); tracked for v0.2."
            )
            return 2

        entry = _resolve_entry(ext_path, kind=kind, name=name)
        if entry is None:
            con.print(
                f"axi ext run: capability {args.capability!r} not declared in "
                f"{ext_path}/axiom-extension.toml"
            )
            return 1

        try:
            target = _load_entry(entry)
        except Exception as exc:  # noqa: BLE001 — surface cleanly
            con.print(f"axi ext run: failed to import entry {entry!r}: {exc}")
            return 1

        # argparse.REMAINDER may leave a leading ``--`` in place.
        cap_args = list(args.cap_args or [])
        if cap_args and cap_args[0] == "--":
            cap_args = cap_args[1:]

        try:
            result = target(cap_args) if callable(target) else None
        except SystemExit as exc:  # CLI entry points often sys.exit
            return int(exc.code or 0)
        except Exception as exc:  # noqa: BLE001
            con.print(f"axi ext run: capability raised {type(exc).__name__}: {exc}")
            return 1

        if isinstance(result, int):
            return result
        return 0


__all__ = ["RunProvider"]


# Ensure the installed-path resolver is importable via its test seam.
if "axiom.cli.ext.commands.run" not in sys.modules:  # pragma: no cover
    # Harmless guard — keeps `patch(...)` reliable for module-level lookups.
    pass
