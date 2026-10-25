# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Regression: the default append path must index into the recall corpus.

The MCP ``axiom_memory_append`` and CLI ``axi memory record`` write paths build
their own default ``CompositionService`` via ``_build_default_composition``. If
that builder omits the ``recall_index`` (as it did — while
``build_default_serving_service`` carried one), appended memory lands in the
ledger but is never projected into the recall corpus, so ``recall`` returns
``served=0`` for everything a tool ever logged. This test pins the round-trip:
append through the real default path, then recall through the real default
serving service, over an isolated state dir.
"""

import axiom.infra.paths as paths
from axiom.extensions.builtins.memory.mcp_server import append, recall


def test_appended_memory_is_recallable(tmp_path, monkeypatch):
    # Redirect the user state dir so both append and recall use a throwaway
    # store (never touch the developer's live ledger).
    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path)

    principal = "roundtrip@test"
    # Distinctive, shared literal terms so the sparse/FTS leg matches even when
    # no embedder is available in CI (dense leg is a bonus when it is).
    append(
        tool="claude-code",
        principal_id=principal,
        summary="the flux capacitor calibration code is ZXCVB and it is delicate",
        user_input="what is the flux capacitor calibration code?",
        assistant_output="the flux capacitor calibration code is ZXCVB",
    )

    result = recall(
        query="flux capacitor calibration code",
        principal_id=principal,
        k=5,
    )

    # The gate is not the issue here (own-principal recall passes post-OQ-A2-1);
    # the fragment must actually be in the corpus to be served.
    assert result["served"] >= 1, (
        "appended memory was not indexed into the recall corpus "
        f"(served={result['served']}, denied={result['denied']}, "
        f"degraded={result['degraded']}) — the default write builder is "
        "missing a recall_index"
    )
    joined = " ".join(
        (f.get("text") or f.get("summary") or "")
        for f in result.get("fragments", [])
    )
    assert "ZXCVB" in joined
