# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext graph`` — Mermaid visualization of extension capabilities + deps.

The output is a ``flowchart TD`` (project convention: vertical layout;
every node + subgraph styled) showing:

- One subgraph per extension, labeled with the extension name.
- One node per declared ``[[extension.provides]]`` capability inside the
  subgraph.
- Dependency edges drawn from the extension to entries in
  ``[extension.compatibility]`` (e.g. ``axiom >= 0.10``) and to each
  ``[[extension.consumes]]`` block.

In ``--installed`` mode the same logic runs against every discovered
extension, so the resulting graph is the full dependency web.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from axiom.cli.ext._output import console, error, status
from axiom.cli.ext.provider import CliContext

# ---------------------------------------------------------------------------
# Mermaid palette — matches project convention (graphite + burnt-orange accent)
# ---------------------------------------------------------------------------

# Extension subgraph fills. Lighter fill + dark text so the labels stay legible
# when rendered against the common white README background.
_EXT_FILL = "#F5F3EF"
_EXT_STROKE = "#3C3C3B"
_EXT_TEXT = "#1F1F1E"

# Capability nodes: the accent (UT burnt orange) signals "this extension
# produces this".
_CAP_FILL = "#BF5700"
_CAP_STROKE = "#6B2F00"
_CAP_TEXT = "#FFFFFF"

# Dependency nodes: neutral stone grey — signals "external to the graph".
_DEP_FILL = "#D7D2C8"
_DEP_STROKE = "#6B655C"
_DEP_TEXT = "#1F1F1E"


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------


_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def _slug(raw: str) -> str:
    """Mermaid-safe identifier derived from ``raw``. Collisions are caller risk."""
    cleaned = _SAFE_SLUG_RE.sub("_", raw).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = "n_" + cleaned
    return cleaned


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def _load_manifest(ext_path: Path) -> dict[str, Any]:
    path = ext_path / "axiom-extension.toml"
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _provides_label(block: dict[str, Any]) -> str:
    kind = block.get("kind", "?")
    label = (
        block.get("name")
        or block.get("noun")
        or block.get("integration")
        or ", ".join(block.get("events", []) or [])
        or ", ".join(block.get("names", []) or [])
        or "(unnamed)"
    )
    return f"{kind}: {label}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_graph(ext_paths: list[Path]) -> str:
    """Render a Mermaid ``flowchart TD`` graph for the given extensions.

    ``ext_paths`` must point at extension roots (each containing an
    ``axiom-extension.toml``). Dependencies are drawn as shared nodes
    across the top-level namespace so the same dep (``axiom``, ``python``)
    collapses to a single node when referenced by multiple extensions.
    """
    lines: list[str] = ["flowchart TD"]
    style_lines: list[str] = []
    seen_deps: set[str] = set()
    seen_caps: set[str] = set()

    for ext_path in ext_paths:
        try:
            manifest = _load_manifest(ext_path)
        except Exception:  # noqa: BLE001 — skip unreadable manifests
            continue

        ext_block = manifest.get("extension", {}) or {}
        ext_name = ext_block.get("name") or ext_path.name
        ext_slug = _slug(ext_name)

        # Subgraph per extension
        lines.append(f"    subgraph ext_{ext_slug}[\"{ext_name}\"]")
        # Always include a central identity node so lint/graph viewers
        # render something even for extensions with no capabilities.
        ident_slug = f"{ext_slug}_self"
        version = ext_block.get("version", "")
        ident_label = f"{ext_name}" + (f"<br/>v{version}" if version else "")
        lines.append(f"        {ident_slug}([\"{ident_label}\"])")
        style_lines.append(
            f"    style {ident_slug} fill:{_EXT_FILL},stroke:{_EXT_STROKE},"
            f"color:{_EXT_TEXT}"
        )

        # Capability nodes
        for i, block in enumerate(ext_block.get("provides", []) or []):
            cap_slug = f"{ext_slug}_cap_{i}"
            if cap_slug in seen_caps:
                continue
            seen_caps.add(cap_slug)
            label = _provides_label(block)
            lines.append(f"        {cap_slug}[\"{label}\"]")
            lines.append(f"        {ident_slug} --> {cap_slug}")
            style_lines.append(
                f"    style {cap_slug} fill:{_CAP_FILL},stroke:{_CAP_STROKE},"
                f"color:{_CAP_TEXT}"
            )

        lines.append("    end")
        style_lines.append(
            f"    style ext_{ext_slug} fill:{_EXT_FILL},stroke:{_EXT_STROKE},"
            f"color:{_EXT_TEXT}"
        )

        # Dependency edges — compatibility + consumes
        compat = ext_block.get("compatibility", {}) or {}
        for dep_name, constraint in compat.items():
            if dep_name == "platforms":
                continue  # not a dependency per se
            dep_slug = f"dep_{_slug(dep_name)}"
            if dep_slug not in seen_deps:
                seen_deps.add(dep_slug)
                label = f"{dep_name} {constraint}".strip()
                lines.append(f"    {dep_slug}[({label})]")
                style_lines.append(
                    f"    style {dep_slug} fill:{_DEP_FILL},stroke:{_DEP_STROKE},"
                    f"color:{_DEP_TEXT}"
                )
            lines.append(f"    {ident_slug} -.-> {dep_slug}")

        for block in ext_block.get("consumes", []) or []:
            pkg = block.get("package", "")
            if not pkg:
                continue
            dep_slug = f"dep_{_slug(pkg)}"
            if dep_slug not in seen_deps:
                seen_deps.add(dep_slug)
                label = pkg
                version = block.get("version")
                if version:
                    label = f"{pkg} {version}"
                lines.append(f"    {dep_slug}[({label})]")
                style_lines.append(
                    f"    style {dep_slug} fill:{_DEP_FILL},stroke:{_DEP_STROKE},"
                    f"color:{_DEP_TEXT}"
                )
            lines.append(f"    {ident_slug} -.-> {dep_slug}")

    lines.extend(style_lines)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Installed-extension enumeration
# ---------------------------------------------------------------------------


def _installed_extension_paths() -> list[Path]:
    """Return extension roots discoverable in the active environment.

    We reuse :func:`axiom.extensions.discovery.discover_extensions` so the
    graph matches exactly what ``axi ext list`` shows.
    """
    try:
        from axiom.extensions.discovery import discover_extensions
    except Exception:  # noqa: BLE001 — allow graph to degrade gracefully
        return []
    out: list[Path] = []
    try:
        for ext in discover_extensions():
            root = getattr(ext, "root", None)
            if root is not None:
                out.append(Path(root))
    except Exception:  # noqa: BLE001
        return []
    return out


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class GraphProvider:
    """Built-in provider for ``axi ext graph [<path>] [--installed]``."""

    verb = "graph"
    description = "Emit a Mermaid flowchart of extension capabilities + dependencies"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Path to the extension (default: current working directory)",
        )
        parser.add_argument(
            "--installed",
            action="store_true",
            help="Graph every installed extension instead of one path",
        )
        parser.add_argument(
            "--output",
            default=None,
            help="Write Mermaid to this path instead of stdout",
        )
        parser.add_argument(
            "--svg",
            dest="svg_path",
            default=None,
            help="Render SVG to this path via mermaid-cli (mmdc)",
        )
        parser.add_argument(
            "--render",
            action="store_true",
            help="Render SVG to a temp file and open it in the default viewer",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        con = console()
        if args.installed:
            ext_paths = _installed_extension_paths()
        else:
            target = Path(args.path).resolve() if args.path else context.cwd
            if not (target / "axiom-extension.toml").exists():
                con.print(
                    f"axi ext graph: {target} is not an extension "
                    "(no axiom-extension.toml); pass a path or use --installed"
                )
                return 1
            ext_paths = [target]

        mermaid = render_graph(ext_paths)

        # -- SVG / render paths --------------------------------------------
        if args.svg_path or args.render:
            svg_target = _resolve_svg_target(args.svg_path)
            if svg_target is None:
                return 2
            rc = _render_svg(mermaid, svg_target)
            if rc != 0:
                return rc
            con.print(f"axi ext graph: wrote {svg_target}")
            if args.render:
                _open_in_viewer(svg_target)
            return 0

        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(mermaid, encoding="utf-8")
            con.print(f"axi ext graph: wrote {out_path}")
            return 0
        # Print the mermaid verbatim — avoid Rich markup interpretation on
        # ``[...]`` contents. ``markup=False`` treats the text as literal.
        con.print(mermaid, markup=False, highlight=False)
        return 0


# ---------------------------------------------------------------------------
# SVG rendering helpers
# ---------------------------------------------------------------------------


def _resolve_svg_target(explicit: str | None) -> Path | None:
    """Return the SVG target path for --svg / --render.

    When ``explicit`` is ``None`` we derive a ``/tmp/axi-ext-graph-<pid>.svg``
    path. Returns ``None`` only if parent-dir creation fails — callers
    translate that to exit 2 with an appropriate error.
    """
    if explicit:
        target = Path(explicit)
    else:
        target = Path(tempfile.gettempdir()) / f"axi-ext-graph-{os.getpid()}.svg"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        error(f"axi ext graph: cannot create {target.parent}: {exc}")
        return None
    return target


def _render_svg(mermaid: str, target: Path) -> int:
    """Pipe Mermaid through ``mmdc`` to write ``target``; return an exit code."""
    mmdc = shutil.which("mmdc")
    if mmdc is None:
        error(
            "axi ext graph: mmdc (mermaid-cli) not found on PATH",
            hint="install via `npm install -g @mermaid-js/mermaid-cli`",
        )
        return 2
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".mmd", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(mermaid)
        mmd_path = fh.name
    try:
        proc = subprocess.run(
            [mmdc, "-i", mmd_path, "-o", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        error("axi ext graph: failed to invoke mmdc")
        return 2
    finally:
        try:
            os.unlink(mmd_path)
        except OSError:
            pass
    if proc.returncode != 0:
        error(
            f"axi ext graph: mmdc exited {proc.returncode}",
            hint=(proc.stderr or proc.stdout or "").strip() or None,
        )
        return 1
    return 0


def _open_in_viewer(path: Path) -> None:
    """Open ``path`` in the system default viewer.

    darwin: ``open`` / linux: ``xdg-open`` / windows: print and let the user
    click it (no built-in on CI Windows).
    """
    cmd: list[str] | None
    if sys.platform == "darwin":
        cmd = ["open", str(path)]
    elif sys.platform.startswith("linux"):
        cmd = ["xdg-open", str(path)]
    else:
        cmd = None
    if cmd is None:
        status("info", "render", f"open manually: {path}")
        return
    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        status("info", "render", f"opener not found; path: {path}")


__all__ = ["GraphProvider", "render_graph"]
