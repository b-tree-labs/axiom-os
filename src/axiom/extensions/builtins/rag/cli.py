# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axi rag — thin extension wrapper delegating to axiom.rag.cli."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    from axiom.rag.cli import main as rag_main

    rag_main(argv)


#: The rag subcommand this alias forwards to. Named so the test below can
#: assert it against the parser rather than trusting a string literal.
_RAG_SEARCH_SUBCOMMAND = "search"


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

    # Delegate to `rag search <args...>`. Anything the user typed after
    # `axi search` becomes the query (and any flags rag's search parser
    # understands, e.g. --top-k, --json, are passed through unchanged).
    #
    # This said "query" for a subcommand that is spelled "search", so the
    # user-facing alias — the one non-developers are meant to reach for —
    # died on argparse's "invalid choice: 'query'" and printed the internal
    # parser's whole verb list at them.
    if argv is None:
        # sys.argv[1:] still carries the verb the user typed, so prepending
        # the subcommand produced ["search", "search", <query>] and every
        # `axi search X` actually searched for "search X" — quietly worse
        # results, with nothing to indicate why.
        argv = sys.argv[1:]
        if argv and argv[0] == _RAG_SEARCH_SUBCOMMAND:
            argv = argv[1:]
    rag_main([_RAG_SEARCH_SUBCOMMAND, *argv])


if __name__ == "__main__":
    main(sys.argv[1:])
