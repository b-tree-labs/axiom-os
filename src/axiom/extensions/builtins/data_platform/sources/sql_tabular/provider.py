# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``SqlTabularProvider`` — the ``sql-tabular`` kind's SourceKindProvider.

The DSN is NOT a kind param — it is the platform-generic
``ConnectorConfig.credential_ref`` (a SecretRef URL), resolved through the
secrets extension at construct/preflight time so the connection string never
lands in the connector TOML. ``preflight`` is where the whole "reachable only
across a narrow network boundary" problem collapses into a checklist: it
resolves the secret, opens the connection **read-only**, and runs a single-row
sample of the extract — each an actionable :class:`PreflightCheck`.
"""

from __future__ import annotations

import argparse

from ...agents.plinth.connectors import ConnectorConfig
from .source import SqlTabularSource


def _sample_query(query: str) -> str:
    """Wrap the extract to fetch one row cheaply for preflight."""
    q = query.rstrip().rstrip(";")
    return f"SELECT * FROM ({q}) AS _preflight LIMIT 1"


class SqlTabularProvider:
    """``sql-tabular`` source kind."""

    kind = "sql-tabular"
    shape = "tabular"
    description = "Tabular rows from a read-only SQL extract (DSN via --credential-ref)"

    def add_register_args(self, subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--query", required=True,
                               help="read-only extract SQL (a SELECT/WITH statement)")
        subparser.add_argument("--schema-ref", required=True,
                               help="declared schema id these rows fill")

    def params_from_args(self, args: argparse.Namespace) -> dict[str, str]:
        return {"query": args.query, "schema_ref": args.schema_ref}

    def validate(self, config: ConnectorConfig) -> list[str]:
        errors: list[str] = []
        query = (config.params.get("query") or "").strip()
        if not query:
            errors.append("sql-tabular requires --query")
        if not config.params.get("schema_ref"):
            errors.append("sql-tabular requires --schema-ref")
        if not config.credential_ref:
            errors.append(
                "sql-tabular requires --credential-ref (a SecretRef URL resolving to "
                "the DSN, e.g. env://SHADOW_DB_DSN or openbao://host/db/dsn)"
            )
        if query and not query.lower().startswith(("select", "with")):
            errors.append("sql-tabular --query must be a read-only SELECT/WITH statement")
        return errors

    def construct(self, config: ConnectorConfig) -> SqlTabularSource:
        return SqlTabularSource(
            name=config.name,
            dsn=self._resolve_dsn(config),
            query=config.params["query"],
            schema_ref=config.params["schema_ref"],
        )

    def _resolve_dsn(self, config: ConnectorConfig) -> str:
        from axiom.extensions.builtins.secrets import SecretRef, resolve

        if not config.credential_ref:
            raise ValueError(f"connector {config.name!r}: missing credential_ref (the DSN secret-ref)")
        with resolve(SecretRef.parse(config.credential_ref)) as secret:
            return secret.as_str()

    def preflight(self, config: ConnectorConfig):
        from ..contracts import PreflightCheck, PreflightResult

        checks: list[PreflightCheck] = []

        # 1. credential present + resolvable ---------------------------------
        if not config.credential_ref:
            checks.append(PreflightCheck(
                name="Credential", ok=False,
                message="No DSN credential is configured.",
                remediation="Register with --credential-ref pointing at the DSN secret "
                            "(e.g. env://SHADOW_DB_DSN or openbao://host/db/dsn).",
                actor="admin",
            ))
            return PreflightResult(connector=config.name, kind=self.kind, checks=checks)
        try:
            dsn = self._resolve_dsn(config)
            checks.append(PreflightCheck(name="Credential", ok=True,
                                         message="Resolved the DSN secret."))
        except Exception as exc:  # noqa: BLE001
            checks.append(PreflightCheck(
                name="Credential", ok=False,
                message=f"Could not resolve {config.credential_ref}: {exc}",
                remediation="Confirm the secret exists and this host can read it "
                            "(secrets backend / env var).",
                copy_value=config.credential_ref, actor="admin",
            ))
            return PreflightResult(connector=config.name, kind=self.kind, checks=checks)

        # 2. reachable + authenticates (read-only connect) -------------------
        try:
            import psycopg

            conn = psycopg.connect(dsn, autocommit=True, connect_timeout=15)
        except Exception as exc:  # noqa: BLE001
            checks.append(PreflightCheck(
                name="Reachability", ok=False,
                message=f"Could not connect: {exc}",
                remediation="Confirm the DB host:port is reachable FROM THIS HOST "
                            "(open the port to this host's egress IP) and the credential "
                            "is valid.",
                actor="admin",
            ))
            return PreflightResult(connector=config.name, kind=self.kind, checks=checks)

        # 3. the extract runs read-only + returns a sample row ---------------
        try:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(_sample_query(config.params.get("query", "")))
                row = cur.fetchone()
            checks.append(PreflightCheck(name="Reachability", ok=True,
                                         message="Connected read-only and ran the extract."))
            checks.append(PreflightCheck(
                name="Sample row", ok=row is not None,
                message="The extract returned at least one row." if row is not None
                        else "Connected, but the extract returned no rows.",
                remediation="" if row is not None
                            else "Confirm the query and that the source table has data.",
                actor="you",
            ))
        except Exception as exc:  # noqa: BLE001
            checks.append(PreflightCheck(
                name="Extract query", ok=False,
                message=f"Connected, but the query failed: {exc}",
                remediation="Check the --query SQL against the source schema.",
                actor="you",
            ))
        finally:
            conn.close()

        return PreflightResult(connector=config.name, kind=self.kind, checks=checks)


__all__ = ["SqlTabularProvider"]
