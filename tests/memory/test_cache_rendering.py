# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Cache-aware two-zone rendering + one-shot write-back (ADR-087 D6 / F5).

Two-zone layout: an epoch-pinned, content-hashed preamble in the stable prefix
with a cache breakpoint after it; per-turn recall only in the volatile tail
after conversation history. Byte-identical rendering (canonical order, no
timestamps; a no-op sync writes nothing). Session injection ledger + hysteresis
(never re-serve what's in context; prefer previously-pinned on ties). Hard
cadence: instruction-file write-back only at session boundary / epoch rollover.

The cache A/B is a token-accounting proxy (no paid cache-billing provider in
CI): (a) preamble byte-identical across turns, (b) breakpoint after preamble,
(c) recall only in the tail, (d) cached-turn prefix token count invariant vs a
naive-injection comparison that reshuffles the prefix every turn.
"""

from __future__ import annotations

import pytest

from axiom.memory.rendering import (
    EPOCH_ROLLOVER,
    SESSION_BOUNDARY,
    InjectionLedger,
    InstructionFileWriteBack,
    WriteBackRefused,
    count_tokens,
    pin_epoch,
    render_naive_prefix,
    render_two_zone,
    select_preamble,
)
from axiom.memory.serving import ServableItem
from axiom.vega.federation.policy import ClassificationStamp, VisibilityHorizon


def _item(fid, text) -> ServableItem:
    return ServableItem(
        fragment_id=fid,
        cognitive_type="semantic",
        visibility=VisibilityHorizon.PUBLIC.value,
        classification=ClassificationStamp.unclassified().to_dict(),
        account="@alice:work",
        text=text,
    )


class TestEpochPinningByteIdentical:
    def test_preamble_is_canonically_ordered_and_untimestamped(self):
        snap = pin_epoch(
            "session://s1", 0,
            [_item("b", "second fact"), _item("a", "first fact")],
        )
        text = snap.render()
        # Canonical order = by fragment id (a before b), stable regardless of
        # input order.
        assert text.index("first fact") < text.index("second fact")
        assert "T00:00:00" not in text and "+00:00" not in text

    def test_same_snapshot_renders_byte_identical(self):
        items = [_item("a", "alpha"), _item("b", "beta")]
        s1 = pin_epoch("session://s1", 0, items)
        s2 = pin_epoch("session://s1", 0, list(reversed(items)))
        assert s1.render() == s2.render()
        assert s1.content_hash == s2.content_hash

    def test_content_hash_changes_only_on_content_change(self):
        base = pin_epoch("session://s1", 0, [_item("a", "alpha")])
        same = pin_epoch("session://s1", 0, [_item("a", "alpha")])
        diff = pin_epoch("session://s1", 0, [_item("a", "alpha changed")])
        assert base.content_hash == same.content_hash
        assert base.content_hash != diff.content_hash


class TestTwoZoneLayout:
    def test_breakpoint_sits_after_preamble(self):
        snap = pin_epoch("session://s1", 0, [_item("a", "stable fact")])
        rendered = render_two_zone(
            snap, history="user: hi\nassistant: hello",
            tail_items=[_item("t1", "fresh recall about coffee")],
        )
        assert rendered.breakpoint_char == len(rendered.preamble)
        # Everything before the breakpoint is exactly the pinned preamble.
        assert rendered.full()[: rendered.breakpoint_char] == rendered.preamble

    def test_recall_only_in_volatile_tail_not_preamble(self):
        snap = pin_epoch("session://s1", 0, [_item("a", "stable fact")])
        rendered = render_two_zone(
            snap, history="conversation so far",
            tail_items=[_item("t1", "fresh recall about coffee")],
        )
        assert "fresh recall about coffee" in rendered.tail
        assert "fresh recall about coffee" not in rendered.preamble
        # Tail comes after conversation history.
        full = rendered.full()
        assert full.index("conversation so far") < full.index("fresh recall about coffee")


class TestCacheProxyAB:
    def test_two_zone_prefix_token_count_invariant_vs_naive(self):
        """The cache A/B token-accounting proxy (per the knob)."""
        snap = pin_epoch(
            "session://s1", 0,
            [_item("a", "alice prefers dark roast"), _item("b", "meetings blocked am")],
        )
        # Three turns, each with different per-turn recall.
        per_turn_recall = [
            [_item("r1", "recall one about tea")],
            [_item("r2", "recall two about travel"), _item("r3", "recall three")],
            [_item("r4", "recall four about the budget review")],
        ]
        histories = ["h1", "h1 h2", "h1 h2 h3"]

        two_zone_prefix_tokens = []
        naive_prefix_tokens = []
        preambles = []
        for turn in range(3):
            rendered = render_two_zone(
                snap, history=histories[turn], tail_items=per_turn_recall[turn],
            )
            preambles.append(rendered.preamble)
            two_zone_prefix_tokens.append(count_tokens(rendered.prefix))
            naive = render_naive_prefix(snap, per_turn_recall[turn])
            naive_prefix_tokens.append(count_tokens(naive))

        # (a) preamble byte-identical across turns
        assert len(set(preambles)) == 1
        # (d) two-zone prefix token count is invariant across turns
        assert len(set(two_zone_prefix_tokens)) == 1
        # ...while naive injection reshuffles the prefix → it varies (cache miss)
        assert len(set(naive_prefix_tokens)) > 1


class TestInjectionLedgerHysteresis:
    def test_tail_never_re_serves_in_context_fragments(self):
        ledger = InjectionLedger(in_context={"a", "b"})
        candidates = [_item("a", "already in preamble"), _item("c", "brand new")]
        tail = ledger.select_tail(candidates, limit=5)
        assert [i.fragment_id for i in tail] == ["c"]

    def test_hysteresis_prefers_previously_pinned_on_ties(self):
        ledger = InjectionLedger(in_context=set(), previously_pinned={"old"})
        # "old" and "new" tie on score; hysteresis keeps "old" for a stable set.
        scored = [(_item("new", "n"), 1.0), (_item("old", "o"), 1.0)]
        chosen = select_preamble(scored, ledger, limit=1)
        assert [i.fragment_id for i in chosen] == ["old"]

    def test_higher_score_still_wins_over_hysteresis(self):
        ledger = InjectionLedger(in_context=set(), previously_pinned={"old"})
        scored = [(_item("new", "n"), 2.0), (_item("old", "o"), 1.0)]
        chosen = select_preamble(scored, ledger, limit=1)
        assert [i.fragment_id for i in chosen] == ["new"]


class TestWriteBackCadence:
    def test_mid_session_write_back_refused(self, tmp_path):
        wb = InstructionFileWriteBack(path=tmp_path / "AGENTS.md")
        snap = pin_epoch("session://s1", 0, [_item("a", "durable preference")])
        with pytest.raises(WriteBackRefused):
            wb.sync(snap, cadence="mid_session")

    def test_session_boundary_write_back_allowed(self, tmp_path):
        path = tmp_path / "AGENTS.md"
        wb = InstructionFileWriteBack(path=path)
        snap = pin_epoch("session://s1", 0, [_item("a", "durable preference")])
        wrote = wb.sync(snap, cadence=SESSION_BOUNDARY)
        assert wrote is True
        assert "durable preference" in path.read_text()

    def test_epoch_rollover_write_back_allowed(self, tmp_path):
        path = tmp_path / "AGENTS.md"
        wb = InstructionFileWriteBack(path=path)
        snap = pin_epoch("session://s1", 1, [_item("a", "durable preference")])
        assert wb.sync(snap, cadence=EPOCH_ROLLOVER) is True

    def test_no_op_sync_writes_nothing(self, tmp_path):
        path = tmp_path / "AGENTS.md"
        wb = InstructionFileWriteBack(path=path)
        snap = pin_epoch("session://s1", 0, [_item("a", "durable preference")])
        assert wb.sync(snap, cadence=SESSION_BOUNDARY) is True
        mtime = path.stat().st_mtime_ns
        # Re-syncing the same content must not touch the file.
        assert wb.sync(snap, cadence=SESSION_BOUNDARY) is False
        assert path.stat().st_mtime_ns == mtime
