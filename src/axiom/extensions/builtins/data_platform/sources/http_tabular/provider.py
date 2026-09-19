# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``HttpTabularProvider`` — the ``http-tabular`` kind's SourceKindProvider.

A tabular kind (``shape = "tabular"``): its ``construct`` returns a
:class:`HttpTabularSource` (rows), and its ``preflight`` GETs the endpoint and
reports reachability / parse / sample-row as plain-language checks.
"""

from __future__ import annotations

import argparse

from ...agents.plinth.connectors import ConnectorConfig
from . import source as _source
from .source import HttpTabularSource


class HttpTabularProvider:
    """``http-tabular`` source kind."""

    kind = "http-tabular"
    shape = "tabular"
    description = "Tabular rows from a CSV/JSON endpoint over HTTP(S)"

    def add_register_args(self, subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--url", required=True,
                               help="CSV/JSON endpoint URL the source fetches")
        subparser.add_argument("--format", choices=["csv", "json"], default="csv",
                               help="payload format (default: csv)")
        subparser.add_argument("--schema-ref", required=True,
                               help="declared schema id these rows fill")

    def params_from_args(self, args: argparse.Namespace) -> dict[str, str]:
        return {"url": args.url, "format": args.format, "schema_ref": args.schema_ref}

    def validate(self, config: ConnectorConfig) -> list[str]:
        errors: list[str] = []
        p = config.params
        if not p.get("url"):
            errors.append("http-tabular requires --url")
        if p.get("format") not in (None, "csv", "json"):
            errors.append("http-tabular --format must be csv or json")
        if not p.get("schema_ref"):
            errors.append("http-tabular requires --schema-ref")
        return errors

    def construct(self, config: ConnectorConfig) -> HttpTabularSource:
        p = config.params
        return HttpTabularSource(
            name=config.name,
            url=p["url"],
            fmt=p.get("format", "csv"),
            schema_ref=p["schema_ref"],
        )

    def preflight(self, config: ConnectorConfig):
        from ..contracts import PreflightCheck, PreflightResult

        checks: list[PreflightCheck] = []
        p = config.params
        url = p.get("url", "")
        fmt = p.get("format", "csv")

        try:
            raw, _ = _source._http_get(url, timeout=15)
        except Exception as exc:  # noqa: BLE001 — surface as a check, not a crash
            checks.append(PreflightCheck(
                name="Reachability", ok=False,
                message=f"Could not reach the endpoint: {exc}",
                remediation="Confirm the URL is correct and reachable from THIS host "
                            "(proxy / VPN / firewall may block it).",
                copy_value=url, actor="admin",
            ))
            return PreflightResult(connector=config.name, kind=self.kind, checks=checks)
        checks.append(PreflightCheck(
            name="Reachability", ok=True,
            message=f"Fetched {len(raw)} bytes from the endpoint.",
        ))

        try:
            rows = _source.parse_rows(raw, fmt)
        except Exception as exc:  # noqa: BLE001
            checks.append(PreflightCheck(
                name="Parse", ok=False,
                message=f"Fetched, but could not parse as {fmt}: {exc}",
                remediation="Check --format matches the endpoint's content (csv vs json).",
                actor="you",
            ))
            return PreflightResult(connector=config.name, kind=self.kind, checks=checks)

        checks.append(PreflightCheck(
            name="Sample rows", ok=bool(rows),
            message=f"Parsed {len(rows)} rows." if rows else "Parsed OK, but zero rows returned.",
            remediation="" if rows else "The endpoint returned no rows — confirm it has data.",
            actor="you",
        ))
        return PreflightResult(connector=config.name, kind=self.kind, checks=checks)


__all__ = ["HttpTabularProvider"]
