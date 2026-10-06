# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What each harness is configured to run, and whether that still works.

``axi mcp status`` described the surface the server *would* publish and said
nothing about the harnesses that call it. So every question people actually
arrived with was unanswerable from it: is this editor configured, which file
says so, what will it run, and why does it show no tools.

This module answers those from the files themselves rather than from a claim.
Two independent sources, not joined:

- **Configuration** — each client's own config file, read back through the
  same reader the installer writes with. A harness we never configured does
  not appear; a harness configured to run an interpreter that no longer
  exists appears with that said plainly, because that failure is otherwise
  silent. The harness shows no tools and reports no error.
- **Starts** — the stamps in :mod:`.runs`, named by how each harness
  identified itself.

Deliberately not joined, because the two name harnesses differently and a
guessed join would be wrong exactly when it mattered. A client's own name is
the better thing to say to a person anyway.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from axiom.extensions.builtins.mcp import runs

__all__ = ["HarnessView", "complaints", "survey"]


def _brand_cli() -> str:
    """The command the reader actually typed.

    A remedy that names a command which does not exist on the reader's machine
    is not a remedy. A consumer distribution renames the CLI, so this is asked
    rather than assumed.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


@dataclass(frozen=True)
class HarnessView:
    """One configured harness, as its own config file describes it."""

    tool: str
    config_path: Path
    command: str
    args: tuple[str, ...] = ()
    command_exists: bool = True

    @property
    def runs_what(self) -> str:
        """The command line this harness will launch, for printing."""
        return " ".join([self.command, *self.args])


def _runnable(command: str) -> bool:
    """Will this resolve when a harness launches it?

    The installer writes an absolute interpreter path, so the usual failure is
    not a missing ``PATH`` entry but a virtualenv that was rebuilt or a Python
    that was upgraded out from under the recorded path. Either way the harness
    launches nothing, lists no tools, and reports no error.
    """
    if not command:
        return False
    p = Path(command)
    if p.is_absolute() or os.sep in command:
        return p.exists()
    from shutil import which

    return which(command) is not None


def survey(*, tools: list[str] | None = None) -> list[HarnessView]:
    """Every harness that has our server in its config, as configured."""
    from axiom.extensions.builtins.mcp.install import (
        config_path_for,
        read_entry,
        resolve_server,
        spec_for,
        supported_tools,
    )

    server_name, _command, _args = resolve_server()
    views: list[HarnessView] = []
    for tool in tools if tools is not None else supported_tools():
        spec = spec_for(tool)
        if spec is None:
            continue
        try:
            path = config_path_for(spec)
            entry = read_entry(spec, server_name, path=path)
        except Exception:  # noqa: BLE001 — a malformed config is reported, below
            views.append(
                HarnessView(
                    tool=tool,
                    config_path=config_path_for(spec),
                    command="",
                    command_exists=False,
                )
            )
            continue
        if entry is None:
            continue
        command = str(entry.get("command") or "")
        args = tuple(str(a) for a in (entry.get("args") or ()))
        views.append(
            HarnessView(
                tool=tool,
                config_path=path,
                command=command,
                args=args,
                command_exists=_runnable(command),
            )
        )
    return sorted(views, key=lambda v: v.tool)


def complaints(
    *,
    views: list[HarnessView] | None = None,
    started: list[runs.Run] | None = None,
    node_root: Path | str | None = None,
    installed: str | None = None,
) -> list[str]:
    """Everything wrong, each with its remedy, in the order to act on it.

    Each sentence names what to do. A finding whose remedy the reader has to
    infer is how "restart the editor" came to be discovered by guessing rather
    than read off a surface.
    """
    the_views = survey() if views is None else views
    out: list[str] = []

    for v in the_views:
        if not v.command:
            out.append(
                f"{v.tool}: its config at {v.config_path} could not be read. "
                f"Re-run `{_brand_cli()} mcp install --tool {v.tool}` to rewrite it."
            )
        elif not v.command_exists:
            out.append(
                f"{v.tool} is configured to run {v.command}, which does not exist "
                f"on this machine. It will list no tools and report no error. "
                f"Re-run `{_brand_cli()} mcp install --tool {v.tool}` to point it "
                f"at this interpreter."
            )

    current = installed if installed is not None else runs.installed_version()
    behind = (
        [r for r in started if r.version != current]
        if (started is not None and current)
        else runs.stale_runs(node_root=node_root, installed=current)
    )
    for r in behind:
        out.append(
            f"{r.harness} is serving {r.version} and {current} is installed. "
            f"A harness keeps the server it already started, so restart "
            f"{r.harness} to pick the new one up."
        )
    return out
