# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which data file a read answers from, and who may choose it.

On the CLI the operator is at their own shell and may point any verb at any
file with ``data``. Every other surface — MCP, web, chat — is a caller the
node is serving, and a caller-chosen path there would let it aim the reader
at any JSON file the node process can open, with the validator's defect
list echoing pieces of that file back. So off the CLI the read answers from
the node's own data file, ``<state_dir>/program/data.json``, and a ``data``
param is refused rather than silently ignored.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from axiom.infra.skill_dispatch import CLI_SURFACE
from axiom.infra.skills import SkillContext

if TYPE_CHECKING:
    from ..model import ProgramData

#: The refusal kinds a program read reports in ``value["refused"]`` so a
#: transport can map them (the HTTP route: 404 / 422 / 503) without parsing
#: error prose.
ABSENT = "absent"
BAD_REQUEST = "bad_request"
NO_DATA = "no_data"

#: The stable path the node serves the program face at (the ``mount.py``
#: MountSpec prefix). It is the *preferred backlink target*: stable across
#: re-renders, unlike a per-artifact URL. The absolute URL needs the node's
#: own public host — and Axiom has **no node-public-URL primitive** today
#: (the HTTP server knows only its bind host/port, not a public address), so
#: the absolute canonical URL stays deployment-config-supplied via
#: ``program.endpoints`` (e.g. an ``endpoints.canonical``). This constant lets
#: a consumer that already knows the node's public host form the backlink; the
#: platform never invents or hardcodes one.
PROGRAM_SERVED_PATH = "/program"

#: The reserved endpoint name a deployment uses to declare the program's own
#: stable public home — the preferred absolute backlink target. Other named
#: endpoints (``tracker_site``, ``roadmap``, …) are additional link-outs.
CANONICAL_ENDPOINT = "canonical"


def default_data_path(ctx: SkillContext) -> Path:
    return ctx.state_dir / "program" / "data.json"


def resolve_data_path(params: dict[str, Any], ctx: SkillContext) -> tuple[Path | None, str | None]:
    """``(path, None)`` to read, or ``(None, refusal message)``."""
    chosen = params.get("data")
    if chosen:
        if getattr(ctx, "surface", None) != CLI_SURFACE:
            return None, (
                "the data param names a file and is accepted on the CLI only; "
                f"on the {ctx.surface or 'unspecified'} surface the program read answers "
                "from the node's own data file"
            )
        return Path(chosen), None
    return default_data_path(ctx), None


def resolve_endpoints(data: ProgramData, ctx: SkillContext | None = None) -> dict[str, Any]:
    """The program's canonical endpoints, resolved for any renderer to link out.

    Returns ``{"declared": {...}, "served_path": "/program", "canonical":
    <url|None>}``:

    - ``declared`` — the deployment's ``program.endpoints`` map (names → URLs),
      the stable link-out targets.
    - ``served_path`` — the stable path the node serves the program at. A
      consumer that knows the node's public host forms the absolute backlink
      from it.
    - ``canonical`` — the preferred absolute backlink target. Precedence:
      (1) the node's own served ``/program`` URL, resolved at serve time from a
      node-public-URL primitive — **none exists**, so this contributes nothing
      today; (2) a declared ``endpoints.canonical``. The platform never invents
      a URL, so when neither is available ``canonical`` is ``None`` and the
      backlink stays config-supplied.
    """
    declared = data.endpoints()
    canonical = _node_served_url(ctx) or declared.get(CANONICAL_ENDPOINT)
    return {
        "declared": declared,
        "served_path": PROGRAM_SERVED_PATH,
        "canonical": canonical,
    }


def _node_served_url(ctx: SkillContext | None) -> str | None:
    """The node's own absolute served ``/program`` URL, if the node exposes a
    public base URL. Axiom has no node-public-URL primitive (the HTTP server
    knows only its bind host/port), so this returns ``None`` today — recorded
    as a single, honest seam rather than a hardcoded guess. When such a
    primitive lands, resolve it here and the canonical backlink upgrades from
    config-supplied to node-resolved with no caller change."""
    return None


__all__ = [
    "ABSENT",
    "BAD_REQUEST",
    "NO_DATA",
    "PROGRAM_SERVED_PATH",
    "CANONICAL_ENDPOINT",
    "default_data_path",
    "resolve_data_path",
    "resolve_endpoints",
]
