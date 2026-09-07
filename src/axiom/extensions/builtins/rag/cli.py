# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axi rag — thin extension wrapper delegating to axiom.rag.cli."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    from axiom.rag.cli import main as rag_main

    rag_main(argv)


def main_search(argv: list[str] | None = None) -> None:
    """Entry point for `axi search` — user-facing alias for `rag query`.

    Per the 2026-05-03 design conversation, "RAG" is internal jargon
    most non-developers don't recognize.  Mom-and-pop researchers want
    to *search their stuff*, not learn what retrieval-augmented
    generation is.  This entry surfaces the search experience under
    the `start` intent (universal end-user) while `axi rag` keeps the
    full corpus-management surface for builders.
    """
    from axiom.rag.cli import main as rag_main

    # Delegate to `rag query <args...>`. Anything the user typed after
    # `axi search` becomes the query (and any flags rag's query parser
    # understands, e.g. --top-k, --json, are passed through unchanged).
    if argv is None:
        argv = sys.argv[1:]
    rag_main(["query", *argv])


if __name__ == "__main__":
    main(sys.argv[1:])
