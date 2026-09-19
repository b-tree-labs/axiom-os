# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dedup must outlive the process that set it.

The incident: a watchdog on a 15-minute timer passed a dedup key, documented
itself as "no re-alert storm every 15 min", and delivered 96 identical alerts a
day for weeks. The key was honoured perfectly — the log just did not survive
the fork.
"""

from __future__ import annotations

import json

from axiom.extensions.builtins.notifications.dedup import FileDedupLog

KEY = ("@node-ops:netl", "watchdog:failed:system:2026-08-20")


class TestSurvivesTheProcess:
    def test_a_separate_instance_sees_the_entry(self, tmp_path):
        """Two instances stand in for two subprocess runs of the same timer."""
        p = tmp_path / "dedup.json"
        FileDedupLog(p)[KEY] = "rcpt-first"
        assert FileDedupLog(p)[KEY] == "rcpt-first"
        assert KEY in FileDedupLog(p)

    def test_entries_expire(self, tmp_path):
        p = tmp_path / "dedup.json"
        FileDedupLog(p, ttl=100, now=1000.0)[KEY] = "rcpt-1"
        assert KEY in FileDedupLog(p, now=1050.0)
        assert KEY not in FileDedupLog(p, now=2000.0), (
            "a standing condition should alert again tomorrow, not be silenced forever"
        )

    def test_distinct_keys_do_not_collide(self, tmp_path):
        p = tmp_path / "dedup.json"
        log = FileDedupLog(p)
        log[("@a:x", "k")] = "r1"
        log[("@b:x", "k")] = "r2"
        log[("@a:x", "other")] = "r3"
        assert len(FileDedupLog(p)) == 3

    def test_a_corrupt_file_degrades_to_no_suppression(self, tmp_path):
        """Losing suppression is noisy. Losing the alert is the failure that
        matters, so a bad file must never raise."""
        p = tmp_path / "dedup.json"
        p.write_text("{ not json")
        assert KEY not in FileDedupLog(p)
        FileDedupLog(p)[KEY] = "rcpt-ok"
        assert FileDedupLog(p)[KEY] == "rcpt-ok"

    def test_writes_are_atomic(self, tmp_path):
        """A timer firing mid-write must never read half a file."""
        p = tmp_path / "dedup.json"
        log = FileDedupLog(p)
        for i in range(25):
            log[("@a:x", f"k{i}")] = f"r{i}"
            json.loads(p.read_text())  # parseable after every single write
        assert len(FileDedupLog(p)) == 25

    def test_deletion_and_iteration_round_trip(self, tmp_path):
        p = tmp_path / "dedup.json"
        log = FileDedupLog(p)
        log[KEY] = "r"
        assert list(FileDedupLog(p)) == [KEY]
        del FileDedupLog(p)[KEY]
        assert len(FileDedupLog(p)) == 0


class TestSendPathUsesIt:
    def test_default_context_is_durable_not_a_dict(self):
        from axiom.extensions.builtins.notifications.send import SendContext

        ctx = SendContext.default(rehydrate=False)
        assert isinstance(ctx.dedup_log, FileDedupLog), (
            "an in-memory log is empty for exactly the callers dedup protects"
        )

    def test_a_cross_process_hit_returns_a_receipt_not_a_keyerror(self, tmp_path):
        """The prior receipt lives in another run's memory. Suppression must
        still answer 'yes, that was sent'."""
        from axiom.extensions.builtins.notifications.send import SendContext

        ctx = SendContext.default(rehydrate=False)
        ctx.dedup_log = FileDedupLog(tmp_path / "d.json")
        ctx.dedup_log[("@node-ops:netl", "k")] = "rcpt-from-a-previous-process"

        from axiom.extensions.builtins.notifications.send import (
            Classification,
            NotificationPayload,
            send,
        )

        receipt = send(
            ctx,
            actor="@node-ops:netl",
            recipient="@node-ops:netl",
            payload=NotificationPayload(summary="unit failed"),
            classification=Classification.INTERNAL,
            dedup_key="k",
        )
        assert receipt.outcome == "suppressed_duplicate"
        assert receipt.id == "rcpt-from-a-previous-process"
