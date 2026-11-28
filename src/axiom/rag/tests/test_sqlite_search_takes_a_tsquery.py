# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The SQLite store's search() takes ``tsquery`` like the Postgres store's (#1984 parity).

FTS5 cannot run a Postgres tsquery, so a precise identifier ranking comes back empty
there instead of repeating the broad ranking (which would double-count its hits).
"""

from __future__ import annotations

from axiom.rag.chunker import Chunk
from axiom.rag.sqlite_store import SQLiteRAGStore


def test_a_tsquery_search_is_empty_and_a_plain_one_still_finds(tmp_path):
    s = SQLiteRAGStore(f"sqlite:///{tmp_path}/rag.db")
    s.connect()
    s.upsert_chunks(
        [
            Chunk(
                text="pump inspection interval XR7",
                source_path="a.md",
                source_title="A",
                chunk_index=0,
                start_line=1,
            )
        ]
    )
    assert s.search(query_text="pump inspection", tsquery="xr7") == []
    assert [h.source_path for h in s.search(query_text="pump inspection")] == ["a.md"]
    s.close()
