# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi memory reindex`` / ``memory.reindex_recall`` — corpus backfill.

Pins the one-time migration for fragments that landed in the ledger before the
append→recall index was wired: they must become recallable after a reindex,
for a single principal and for every principal in the ledger. Idempotent.
"""

import axiom.infra.paths as paths
from axiom.extensions.builtins.memory.mcp_server import (
    _build_default_composition,
    append,
    recall,
)
from axiom.extensions.builtins.memory.skills.reindex_recall import reindex_recall
from axiom.memory.recall_projection import recall_corpus_for


def _empty_corpus(principal: str) -> None:
    """Simulate the pre-fix state: fragment in ledger, not in the corpus."""
    comp = _build_default_composition()
    comp.recall_index.store.delete_corpus(recall_corpus_for(principal))


def test_reindex_backfills_a_single_principal(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path)
    principal = "backfill@test"
    append(
        tool="claude-code",
        principal_id=principal,
        summary="the reactor trip setpoint token is QWXYZ and must be preserved",
        user_input="what is the reactor trip setpoint token?",
        assistant_output="the reactor trip setpoint token is QWXYZ",
    )

    _empty_corpus(principal)
    # Ledger has the fragment, but the corpus is empty → recall serves nothing.
    pre = recall(query="reactor trip setpoint token", principal_id=principal, k=5)
    assert pre["served"] == 0

    result = reindex_recall(
        {"composition": _build_default_composition(), "principal": principal},
        None,
    )
    assert result.ok, result.errors
    assert result.value["reindexed"] >= 1
    assert result.value["per_principal"][principal] >= 1

    post = recall(query="reactor trip setpoint token", principal_id=principal, k=5)
    assert post["served"] >= 1
    joined = " ".join(
        (f.get("text") or f.get("summary") or "")
        for f in post.get("fragments", [])
    )
    assert "QWXYZ" in joined


def test_reindex_all_covers_every_principal(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path)
    for principal, token in (("alice@test", "ALPHA7"), ("bob@test", "BRAVO9")):
        append(
            tool="claude-code",
            principal_id=principal,
            summary=f"the shared canary token is {token} for {principal}",
            user_input=f"what is the canary token for {principal}?",
            assistant_output=f"the canary token is {token}",
        )
    _empty_corpus("alice@test")
    _empty_corpus("bob@test")

    result = reindex_recall(
        {"composition": _build_default_composition(), "all": True}, None
    )
    assert result.ok, result.errors
    assert set(result.value["principals"]) == {"alice@test", "bob@test"}
    assert result.value["reindexed"] >= 2

    for principal, token in (("alice@test", "ALPHA7"), ("bob@test", "BRAVO9")):
        r = recall(query="canary token", principal_id=principal, k=5)
        assert r["served"] >= 1
        joined = " ".join(
            (f.get("text") or f.get("summary") or "")
            for f in r.get("fragments", [])
        )
        assert token in joined


def test_reindex_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path)
    principal = "idem@test"
    append(
        tool="claude-code",
        principal_id=principal,
        summary="the idempotency token is IDEM42",
        user_input="what is the idempotency token?",
        assistant_output="the idempotency token is IDEM42",
    )
    first = reindex_recall(
        {"composition": _build_default_composition(), "principal": principal},
        None,
    )
    second = reindex_recall(
        {"composition": _build_default_composition(), "principal": principal},
        None,
    )
    # Same fragment count both times — rebuild drops-and-rebuilds, no dupes.
    assert first.value["per_principal"][principal] == (
        second.value["per_principal"][principal]
    )
    assert recall(query="idempotency token", principal_id=principal, k=5)[
        "served"
    ] >= 1


def test_reindex_requires_principal_or_all(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path)
    result = reindex_recall(
        {"composition": _build_default_composition()}, None
    )
    assert not result.ok
    assert "principal" in " ".join(result.errors).lower()
