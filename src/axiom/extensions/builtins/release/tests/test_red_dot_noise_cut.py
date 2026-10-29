# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the 🔴-noise reduction slice.

Three changes:

  1. Title-swap on close: `close_stale_issue` also edits the issue's
     title, swapping the leading 🔴 emoji for ✅ so closed issues stop
     dominating list views with the alarming red dot.

  2. Heartbeat auto-sweep: when main is currently passing, the
     heartbeat fires `sweep_stale(all_prs=True)` so post-merge stale
     🔴s clear without an operator running the manual sweep.

  3. (Workflow YAML — not Python; tested separately by the workflow
     itself once merged. Not exercised here.)
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Change 1 — title-emoji swap on close
# ---------------------------------------------------------------------------


class TestSwapStatusEmojiInTitle:
    def test_red_dot_swaps_to_green_check(self):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            swap_status_emoji_in_title,
        )
        assert swap_status_emoji_in_title(
            "🔴 CI failed on `refs/pull/197/merge` (abc123)"
        ) == "✅ CI failed on `refs/pull/197/merge` (abc123)"

    def test_title_without_red_dot_unchanged(self):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            swap_status_emoji_in_title,
        )
        assert swap_status_emoji_in_title("Unrelated issue title") == (
            "Unrelated issue title"
        )

    def test_already_swapped_title_idempotent(self):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            swap_status_emoji_in_title,
        )
        # Already-swapped → no change
        assert swap_status_emoji_in_title(
            "✅ CI failed on `main` (abc)"
        ) == "✅ CI failed on `main` (abc)"


class TestCloseStaleIssueAlsoEditsTitle:
    def test_close_sequence_invokes_edit_after_close(self, monkeypatch):
        """The full close path should: 1) issue view (fetch current title),
        2) issue close --comment, 3) issue edit --title <swapped>."""
        from axiom.extensions.builtins.release import pr_check_auto_closer

        calls: list[tuple] = []

        def fake_gh(args):
            calls.append(tuple(args))
            if args[:2] == ["issue", "view"]:
                # Return current title as JSON
                import json
                return json.dumps({
                    "title": "🔴 CI failed on `refs/pull/197/merge` (abc)",
                })
            return ""  # close + edit return empty

        monkeypatch.setattr(pr_check_auto_closer, "_run_gh", fake_gh)
        result = pr_check_auto_closer.close_stale_issue(
            issue_number=199, comment="recovered",
        )
        assert result is True

        # Sequence: view, close, edit
        op_kinds = [c[:2] for c in calls]
        assert ("issue", "view") in op_kinds
        assert ("issue", "close") in op_kinds
        assert ("issue", "edit") in op_kinds

        # The edit carries the swapped title
        edit_call = next(c for c in calls if c[:2] == ("issue", "edit"))
        edit_args = " ".join(edit_call)
        assert "✅" in edit_args
        # And references the issue number (#199)
        assert "199" in edit_args

    def test_edit_failure_does_not_break_close(self, monkeypatch):
        """If the title edit fails for any reason, the close still
        counts as successful — the issue IS closed, the visual
        improvement is best-effort."""
        from axiom.extensions.builtins.release import pr_check_auto_closer

        def fake_gh(args):
            if args[:2] == ["issue", "view"]:
                # Title fetch fails (empty response)
                return ""
            return ""

        monkeypatch.setattr(pr_check_auto_closer, "_run_gh", fake_gh)
        # Must not raise; must return True (close itself was successful)
        result = pr_check_auto_closer.close_stale_issue(
            issue_number=199, comment="x",
        )
        assert result is True


# ---------------------------------------------------------------------------
# Change 2 — heartbeat auto-sweeps post-merge stale 🔴s
# ---------------------------------------------------------------------------


class TestHeartbeatAutoSweep:
    def test_auto_sweep_fires_when_main_passing(self, monkeypatch):
        from axiom.extensions.builtins.release import agent_cli

        sweep_calls = []

        def fake_sweep_stale(**kwargs):
            from axiom.extensions.builtins.release.pr_check_auto_closer import (
                SweepReport,
            )
            sweep_calls.append(kwargs)
            return SweepReport(closed=[])

        monkeypatch.setattr(
            agent_cli, "_auto_sweep_post_merge_stale",
            agent_cli._auto_sweep_post_merge_stale,  # use real fn
        )
        monkeypatch.setattr(
            "axiom.extensions.builtins.release.pr_check_auto_closer.sweep_stale",
            fake_sweep_stale,
        )
        monkeypatch.setattr(
            "axiom.extensions.builtins.release.pr_check_auto_closer.current_main_state",
            lambda: "passing",
        )

        agent_cli._auto_sweep_post_merge_stale()
        assert len(sweep_calls) == 1
        assert sweep_calls[0].get("all_prs") is True

    def test_auto_sweep_silent_when_main_not_passing(self, monkeypatch):
        from axiom.extensions.builtins.release import agent_cli

        sweep_calls = []
        monkeypatch.setattr(
            "axiom.extensions.builtins.release.pr_check_auto_closer.sweep_stale",
            lambda **kw: sweep_calls.append(kw) or
                __import__("axiom.extensions.builtins.release.pr_check_auto_closer",
                           fromlist=["SweepReport"]).SweepReport(closed=[]),
        )
        monkeypatch.setattr(
            "axiom.extensions.builtins.release.pr_check_auto_closer.current_main_state",
            lambda: "failing",
        )

        agent_cli._auto_sweep_post_merge_stale()
        # When main isn't passing, no sweep call should be made
        assert sweep_calls == []
