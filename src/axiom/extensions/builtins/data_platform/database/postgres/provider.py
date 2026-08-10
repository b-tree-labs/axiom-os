# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Postgres :class:`DatabaseKindProvider`.

The reference impl for DP-1. When co-located with the pgvector vector
store (the default for v1), the chart's internal Postgres deploy uses
the ``pgvector/pgvector:pg<MAJOR>`` image so a single instance serves
both layers.

Other database kinds (MySQL, SQLite, cloud-managed) land as sibling
provider packages with no platform-code change.
"""

from __future__ import annotations

import argparse


class PostgresDatabaseProvider:
    kind = "postgres"
    description = "PostgreSQL (pgvector image when co-located with pgvector vector store)"

    # ---- install-time CLI ------------------------------------------------

    def add_install_args(self, parser: argparse.ArgumentParser) -> None:
        # The kind-agnostic --db-mode / --db-dsn flags live on the
        # platform parser; this provider adds only what's
        # postgres-specific.
        parser.add_argument(
            "--db-password",
            default="",
            help="password for the bundled Postgres (--db-mode=internal); "
                 "ignored when --db-mode=external",
        )
        parser.add_argument(
            "--db-password-ref",
            default="",
            dest="db_password_ref",
            help="SecretRef URL whose value is the Postgres password "
                 "(e.g. openbao://kv/data/example-host/dp1/db/password, "
                 "env://NEUT_PG_PASSWORD, kubernetes://axiom-data/dp1-db/password); "
                 "mutually exclusive with --db-password; resolved at install time "
                 "via the secrets extension. SEC-3.",
        )
        parser.add_argument(
            "--db-database",
            default="axiom",
            help="application database name (default: axiom)",
        )
        parser.add_argument(
            "--db-username",
            default="axiom",
            help="application database user (default: axiom)",
        )
        parser.add_argument(
            "--db-storage",
            default="20Gi",
            help="bundled-Postgres PVC size when --db-mode=internal (default: 20Gi)",
        )

    # ---- chart wiring ----------------------------------------------------

    def _resolve_password(self, args: argparse.Namespace) -> str:
        """Pick a password from ``--db-password`` or ``--db-password-ref``.

        ``--db-password`` wins if both are set, but the combination is
        flagged as a mutually-exclusive misuse so SEC-3 operators get a
        clean error. When only ``--db-password-ref`` is set, resolve it
        via the secrets extension at install time.
        """
        literal = getattr(args, "db_password", "") or ""
        ref_url = getattr(args, "db_password_ref", "") or ""
        if literal and ref_url:
            raise ValueError(
                "--db-password and --db-password-ref are mutually exclusive; "
                "choose one"
            )
        if literal:
            return literal
        if not ref_url:
            return ""
        # Resolve through the secrets extension. Imported lazily so the
        # data_platform doesn't hard-depend on it at module-load time.
        from axiom.extensions.builtins.secrets import SecretRef, resolve
        ref = SecretRef.parse(ref_url)
        secret = resolve(ref)
        return secret.as_str()

    def helm_values(self, args: argparse.Namespace) -> dict[str, str]:
        """Render `--set` pairs the install passes to helm."""
        out: dict[str, str] = {
            "database.kind": "postgres",
            "database.mode": getattr(args, "db_mode", "internal"),
        }
        if out["database.mode"] == "external":
            dsn = getattr(args, "db_dsn", "")
            if dsn:
                out["database.external.dsn"] = dsn
        else:
            out["database.internal.username"] = args.db_username
            out["database.internal.database"] = args.db_database
            out["database.internal.storage"] = args.db_storage
            password = self._resolve_password(args)
            if password:
                out["database.internal.password"] = password
        return out

    # ---- DSN -------------------------------------------------------------

    def construct_dsn(self, args: argparse.Namespace) -> str:
        if getattr(args, "db_mode", "internal") == "external":
            return getattr(args, "db_dsn", "")
        # In-cluster DSN — host is the chart's bundled Service.
        host = "axiom-data-platform-database"
        user = args.db_username
        pwd = self._resolve_password(args) or "<password>"
        db = args.db_database
        return f"postgresql://{user}:{pwd}@{host}:5432/{db}"


__all__ = ["PostgresDatabaseProvider"]
