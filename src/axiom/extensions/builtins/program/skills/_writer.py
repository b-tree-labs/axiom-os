# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One writer per program (ADR-248): who may write the data file and publish.

``program.writer`` names the install (its node id) allowed to write the
program's data file, change log and snapshot, and to publish. Every other
install that runs ``sync`` or ``publish`` (a laptop bound to that node, a
second node) runs read-only: it reads and reports what it would change, and
says so. With no ``program.writer`` every install writes, as before.

The install's id is the node name the topology uses (``AXIOM_NODE_NAME``,
else the short host name); ``_node_id`` overrides it for tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def this_install(params: dict[str, Any]) -> str:
    """This install's node id."""
    override = params.get("_node_id")
    if isinstance(override, str) and override:
        return override
    from axiom.infra.topology import this_node

    return this_node()["node"]


def declared_writer(target: Path) -> str | None:
    """``program.writer`` from the data file, read without validation so a
    peek never turns a drift-only file into a refusal."""
    try:
        doc = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    writer = (doc.get("program") or {}).get("writer") if isinstance(doc, dict) else None
    return writer if isinstance(writer, str) and writer else None


def read_only_reason(writer: str | None, params: dict[str, Any]) -> dict[str, Any] | None:
    """``None`` when this install may write; otherwise the ``read_only``
    block a result carries: the writer, this install, and why."""
    if writer is None:
        return None
    here = this_install(params)
    if here.lower() == writer.lower():
        return None
    return {
        "writer": writer,
        "this_install": here,
        "detail": (
            f"program.writer is {writer}; this install ({here}) reads and reports only. "
            "Run it on the writer to change the program."
        ),
    }


__all__ = ["this_install", "declared_writer", "read_only_reason"]
