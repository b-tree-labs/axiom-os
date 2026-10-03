# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""pgvector :class:`VectorStoreProvider` — Postgres-extension-based.

When co-located with ``--db-kind postgres`` (the v1 default), the
chart's bundled Postgres uses the ``pgvector/pgvector:pg<major>``
image so a single instance serves OLTP + vectors. No separate vector
service is deployed.

When the operator picks a non-postgres database, this provider rejects
at install validation — pgvector is a Postgres extension and has no
standalone deployment shape.
"""

from __future__ import annotations

import argparse


class PgvectorVectorStoreProvider:
    kind = "pgvector"
    description = "pgvector (Postgres extension; co-locates with --db-kind postgres)"
    colocates_with_database = ["postgres"]

    def add_install_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--vector-image-tag",
            default="pg16",
            help="pgvector image tag to bundle when co-located (default: pg16)",
        )

    def helm_values(self, args: argparse.Namespace, *, db_kind: str) -> dict[str, str]:
        if db_kind not in self.colocates_with_database:
            raise ValueError(
                f"pgvector vector-store requires a co-locatable database kind "
                f"({self.colocates_with_database}); got --db-kind={db_kind!r}. "
                "Pick a separate vector-store kind (qdrant, weaviate, …) or "
                "use --db-kind postgres."
            )
        return {
            "vectorStore.kind": "pgvector",
            "vectorStore.colocated": "true",
            "database.internal.image.repository": "pgvector/pgvector",
            "database.internal.image.tag": args.vector_image_tag,
        }


__all__ = ["PgvectorVectorStoreProvider"]
