# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Universal interactive review loop.

Provides two review modes that share the same data model:

    **Detailed mode** (default for ``axi pub review``):
        Present items one at a time.  Reviewer decides per-item:
        accept / edit / drop / comment / skip / quit.

    **Quick mode** (``axi pub review --quick`` or ``--approve-all``):
        Show the full document in a pager or dump to stdout,
        then ask for a single top-level verdict.  Optionally open
        $EDITOR for inline annotation before accepting.

Both modes persist to the same ``ReviewSession`` so a reviewer can
start in quick mode, bail out, then resume in detailed mode later.

The runner itself is domain-agnostic.  Domain-specific behaviour
(display formatting, extra commands, post-review actions) lives in
``ReviewAdapter`` implementations.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Protocol

from axiom.review.models import (
    ReviewDecision,
    ReviewItem,
    ReviewSession,
    ReviewSessionStore,
    _now_iso,
)

# ── adapter protocol ─────────────────────────────────────────────────

class ReviewAdapter(Protocol):
    """Domain-specific adapter for a review type.

    Implement this for each kind of reviewable content (drafts,
    corrections, action proposals, etc.).
    """

    def display_item(self, item: ReviewItem, index: int, total: int) -> None:
        """Print one item for the reviewer to consider."""
        ...

    def display_summary(self, items: list[ReviewItem]) -> None:
        """Print the full content for quick-mode overview.

        Default implementation concatenates all items.  Override for
        richer formatting.
        """
        ...

    def get_commands(self) -> dict[str, str]:
        """Return domain-specific commands beyond the standard set.

        Keys are single-char shortcuts; values are help text.
        Example: ``{"E": "Edit in $EDITOR", "D": "Drop section"}``
        """
        ...

    def handle_command(self, cmd: str, item: ReviewItem) -> str | None:
        """Handle a domain-specific command.

        Return a new status string to apply to the item, or ``None``
        to re-prompt (the command did something but didn't resolve the
        item).
        """
        ...

    def on_session_complete(self, session: ReviewSession) -> None:
        """Called when all items have been reviewed (or on quit).

        Use this for post-review side effects: writing approved files,
        printing publish hints, etc.
        """
        ...


# ── standard commands ─────────────────────────────────────────────────

_STANDARD_COMMANDS: dict[str, str] = {
    "A": "Accept",
    "S": "Skip (defer)",
    "Q": "Save & quit",
}


# ── runner ────────────────────────────────────────────────────────────

class ReviewRunner:
    """Universal interactive review loop.

    Parameters
    ----------
    adapter : ReviewAdapter
        Domain-specific display and command handling.
    store : ReviewSessionStore
        Persistence backend.
    reviewer : str
        Name/email of the person reviewing.  Defaults to $USER.
    """

    def __init__(
        self,
        adapter: ReviewAdapter,
        store: ReviewSessionStore,
        reviewer: str = "",
    ):
        self.adapter = adapter
        self.store = store
        self.reviewer = reviewer or os.environ.get("USER", "reviewer")

    # ── public entry points ──────────────────────────────────────────

    def run(self, session: ReviewSession, *, quick: bool = False) -> None:
        """Run the review loop.

        Parameters
        ----------
        session : ReviewSession
            Session to review (may have prior progress).
        quick : bool
            If True, show full content first, then ask for a single
            top-level verdict instead of item-by-item review.
        """
        if quick:
            self._run_quick(session)
        else:
            self._run_detailed(session)

    # ── quick mode ───────────────────────────────────────────────────

    def _run_quick(self, session: ReviewSession) -> None:
        """Fast informal review: show everything, one verdict."""
        reviewed, total = session.progress
        print()
        print("Quick Review")
        print("=" * 70)
        print(f"Source: {session.source}")
        print(f"Items: {total} total, {reviewed} already reviewed")
        print()

        # Let the adapter show the full content
        self.adapter.display_summary(session.items)

        print()
        print("Commands: A=Approve all  E=Edit then approve  R=Reject all  D=Detailed review")
        print()

        while True:
            try:
                response = input("[A/E/R/D] > ").strip().upper()
            except (EOFError, KeyboardInterrupt):
                print("\n\nReview saved.")
                self.store.save(session)
                return

            if response == "A":
                for item in session.items:
                    if item.status == "pending":
                        item.decisions.append(ReviewDecision(
                            reviewer=self.reviewer,
                            status="accepted",
                            channel="cli",
                            decided_at=_now_iso(),
                        ))
                        item.status = item.resolve_status(session.consensus_mode)
                session.last_reviewed_at = _now_iso()
                self.store.save(session)
                self._print_session_summary(session)
                self.adapter.on_session_complete(session)
                return

            elif response == "E":
                # Concatenate all content, open in $EDITOR, then approve
                edited_content = self._open_in_editor(
                    "\n\n".join(i.content for i in session.items)
                )
                if edited_content is not None:
                    # Apply edits proportionally — user edited the whole doc
                    for item in session.items:
                        if item.status == "pending":
                            item.decisions.append(ReviewDecision(
                                reviewer=self.reviewer,
                                status="edited",
                                channel="cli",
                                edited_content=edited_content,
                                decided_at=_now_iso(),
                            ))
                            item.status = "edited"
                    session.last_reviewed_at = _now_iso()
                    self.store.save(session)
                    self._print_session_summary(session)
                    self.adapter.on_session_complete(session)
                    return

            elif response == "R":
                for item in session.items:
                    if item.status == "pending":
                        item.decisions.append(ReviewDecision(
                            reviewer=self.reviewer,
                            status="rejected",
                            channel="cli",
                            decided_at=_now_iso(),
                        ))
                        item.status = "rejected"
                session.last_reviewed_at = _now_iso()
                self.store.save(session)
                self._print_session_summary(session)
                self.adapter.on_session_complete(session)
                return

            elif response == "D":
                # Fall through to detailed mode
                self._run_detailed(session)
                return

            else:
                print("  A/E/R/D")

    # ── detailed mode ────────────────────────────────────────────────

    def _run_detailed(self, session: ReviewSession) -> None:
        """Item-by-item interactive review."""
        total = len(session.items)
        reviewed, _ = session.progress

        # Build command help line
        adapter_cmds = self.adapter.get_commands()
        all_cmds = {**_STANDARD_COMMANDS, **adapter_cmds}
        cmd_help = "  ".join(f"{k}={v}" for k, v in all_cmds.items())
        cmd_keys = "/".join(all_cmds.keys())

        print()
        print("Interactive Review")
        print("=" * 70)
        print(f"Source: {session.source}")
        print(f"Items: {total} total, {reviewed} reviewed, {total - reviewed} remaining")
        print()
        print(f"Commands: {cmd_help}")

        for i, item in enumerate(session.items):
            if item.status != "pending":
                continue

            self.adapter.display_item(item, i, total)

            # Inner command loop — multiple commands on same item
            while True:
                try:
                    response = input(f"\n  [{i + 1}/{total}] [{cmd_keys}] > ").strip().upper()
                except (EOFError, KeyboardInterrupt):
                    print("\n\nReview saved.")
                    session.last_reviewed_at = _now_iso()
                    self.store.save(session)
                    return

                # ── standard commands ─────────────────────────────
                if response == "A":
                    item.decisions.append(ReviewDecision(
                        reviewer=self.reviewer,
                        status="accepted",
                        channel="cli",
                        decided_at=_now_iso(),
                    ))
                    item.status = item.resolve_status(session.consensus_mode)
                    print("  Accepted")
                    break

                elif response == "S":
                    item.decisions.append(ReviewDecision(
                        reviewer=self.reviewer,
                        status="skipped",
                        channel="cli",
                        decided_at=_now_iso(),
                    ))
                    item.status = "skipped"
                    print("  Skipped")
                    break

                elif response == "Q":
                    session.last_reviewed_at = _now_iso()
                    self.store.save(session)
                    self._print_session_summary(session)
                    self.adapter.on_session_complete(session)
                    return

                # ── adapter commands ──────────────────────────────
                elif response in adapter_cmds:
                    result = self.adapter.handle_command(response, item)
                    if result is not None:
                        # Adapter resolved the item
                        item.decisions.append(ReviewDecision(
                            reviewer=self.reviewer,
                            status=result,
                            channel="cli",
                            decided_at=_now_iso(),
                        ))
                        item.status = item.resolve_status(session.consensus_mode)
                        break
                    # else: adapter handled command but item not resolved,
                    # re-prompt for same item

                else:
                    print(f"  {cmd_keys}")

        # All items visited
        session.last_reviewed_at = _now_iso()
        self.store.save(session)
        self._print_session_summary(session)
        self.adapter.on_session_complete(session)

    # ── helpers ──────────────────────────────────────────────────────

    def _print_session_summary(self, session: ReviewSession) -> None:
        """Print a summary of the review session."""
        from collections import Counter

        counts = Counter(i.status for i in session.items)
        total = len(session.items)

        print()
        print("=" * 60)
        print("Review Summary")
        print("=" * 60)
        for status in ["accepted", "edited", "rejected", "skipped", "pending"]:
            n = counts.get(status, 0)
            if n:
                print(f"  {status.capitalize():<12} {n}")
        print(f"  {'Total':<12} {total}")
        print("=" * 60)

    @staticmethod
    def _open_in_editor(content: str) -> str | None:
        """Open content in $EDITOR and return the edited text.

        Returns None if the editor exits non-zero or content is unchanged.
        """
        editor = os.environ.get("EDITOR", "vi")
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".md", delete=False
            ) as f:
                f.write(content)
                tmp_path = f.name

            result = subprocess.run([editor, tmp_path])
            if result.returncode != 0:
                return None

            with open(tmp_path, encoding="utf-8") as f:
                edited = f.read()

            if edited == content:
                return None  # No changes
            return edited
        except Exception:
            return None
        finally:
            try:
                if tmp_path:
                    os.unlink(tmp_path)
            except (OSError, UnboundLocalError):
                pass
