# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``PushProvider`` — the ``push`` source kind (ADR-106).

Registers a connector that exists to be *pushed to*: producers (the DAQ
Transmitter, an egress agent, a script) POST batches to the ingest face under
this connector's name, and the face lands them under this connector's bronze
root, provenance rules, disposition and tier. There is no credential to
preflight and nothing to pull.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ...agents.plinth.connectors import ConnectorConfig
from .source import PushSource


class PushProvider:
    """``push`` source kind — written through the ingest face, never pulled."""

    kind = "push"
    shape = "tabular"
    description = "Push-only connector: producers POST to /ingest or /ingest/rows under this name"

    def add_register_args(self, subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--schema-ref",
            default="",
            help="declared schema id for pushed rows (informational; each batch names its own)",
        )

    def params_from_args(self, args: argparse.Namespace) -> dict[str, str]:
        return {"schema_ref": getattr(args, "schema_ref", "") or ""}

    def validate(self, config: ConnectorConfig) -> list[str]:
        errors: list[str] = []
        if not config.bronze_root:
            errors.append("push requires --bronze-root")
        if config.default_disposition not in ("allow", "quarantine", "exclude"):
            errors.append("push --default-disposition must be allow, quarantine or exclude")
        return errors

    def construct(self, config: ConnectorConfig) -> PushSource:
        return PushSource(config.name, schema_ref=str(config.params.get("schema_ref", "")))

    def preflight(self, config: ConnectorConfig):
        from ..contracts import PreflightCheck, PreflightResult

        root = Path(config.bronze_root)
        checks = [
            PreflightCheck(
                name="Bronze root",
                ok=root.parent.exists(),
                message=f"{root} ({'parent exists' if root.parent.exists() else 'parent missing'})",
                remediation="Create the parent directory (the writer creates the leaf).",
                copy_value=str(root),
                actor="admin",
            ),
            PreflightCheck(
                name="Nothing to pull",
                ok=True,
                message="push connectors are written through the ingest face; "
                "point the producer at /ingest/rows with this connector as `source`.",
            ),
        ]
        return PreflightResult(connector=config.name, kind=self.kind, checks=checks)

    def url_for(self, config: ConnectorConfig, ref_id: str) -> str | None:
        return None


__all__ = ["PushProvider"]
