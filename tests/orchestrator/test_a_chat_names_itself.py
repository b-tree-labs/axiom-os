# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A conversation names itself from its substance, and keeps a name a person gave it.

The title used to be the first user message cut at 60 characters, set once.
A chat opened from a case was named "About case 41 — pump bearing
temperature. Claims: pu" forever. The reference product re-titles after every
reply while the title is automatic, and never after a rename.
"""

from __future__ import annotations

from axiom.infra.orchestrator.session import Session
from axiom.infra.orchestrator.titling import (
    DEFAULT_TITLE,
    MAX_CHARS,
    auto_title,
    compact,
    keywords,
    strip_seed,
)


class TestTheEngine:
    def test_it_names_a_chat_by_its_subject_not_its_scaffolding(self):
        title = auto_title(
            ["What did the flow meter read against its calibration curve this morning?"]
        )
        assert title == "Flow Meter Read Against Calibration"

    def test_a_seeded_opening_is_named_by_its_subject(self):
        assert strip_seed(
            "About case 41 — pump bearing temperature. Claims: bearing_temp is open."
        ) == ("pump bearing temperature")
        assert (
            auto_title(["About case 41 — pump bearing temperature. Claims: x"])
            == "Pump Bearing Temperature"
        )

    def test_deliberate_capitals_are_kept(self):
        assert keywords("compare the API pH and JSON results") == [
            "Compare",
            "API",
            "pH",
            "JSON",
            "Results",
        ]

    def test_a_title_never_cuts_a_word_and_never_exceeds_the_limit(self):
        long = "Alpha " * 20
        out = compact(long)
        assert len(out) <= MAX_CHARS and not out.endswith("Alph")

    def test_nothing_to_go_on_is_the_placeholder(self):
        assert auto_title([]) == DEFAULT_TITLE
        assert auto_title(["what is the"]) == DEFAULT_TITLE

    def test_a_domain_rule_wins_and_a_broken_one_is_skipped(self):
        def broken(text):
            raise RuntimeError("bad rule")

        def backup(text):
            return "Backup Plan" if "backup" in text.lower() else None

        assert (
            auto_title(["Walk me through the backup checklist"], rules=[broken, backup])
            == "Backup Plan"
        )
        assert auto_title(["Disk usage this cycle"], rules=[backup]) == "Disk Usage Cycle"


class TestTheSession:
    def test_it_is_titled_at_the_first_message_and_retitled_by_a_reply(self):
        s = Session()
        s.add_message("user", "hello")
        assert s.title == "Hello"
        s.add_message("assistant", "Hi.")
        s.add_message("user", "Show me the pool temperature trend for last week")
        assert s.title == "Hello"  # between replies the name stays put
        s.add_message("assistant", "Here it is.")
        assert s.title == "Hello Pool Temperature Trend Last"
        assert s.context["title_source"] == "auto"

    def test_a_name_a_person_gave_is_never_overwritten(self):
        s = Session()
        s.add_message("user", "pool temperature")
        s.name("Weekly pool check")
        s.add_message("assistant", "ok")
        s.add_message("user", "now the disk usage")
        s.add_message("assistant", "ok")
        assert s.title == "Weekly pool check"
        assert s.context["title_source"] == "manual"

    def test_the_lock_survives_a_save_and_a_load(self):
        s = Session()
        s.add_message("user", "x y")
        s.name("Kept")
        again = Session.from_dict(s.to_dict())
        again.add_message("assistant", "ok")
        assert again.title == "Kept"


class TestExistingChatsKeepTheirNames:
    def test_a_title_with_no_recorded_source_is_kept(self):
        # Every conversation saved before this engine: renaming those the next
        # time someone speaks in them would be a surprise nobody asked for.
        s = Session(title="old chat")
        s.add_message("user", "old message")
        s.add_message("assistant", "ok")
        assert s.title == "old chat"

    def test_the_placeholder_is_still_ours_to_replace(self):
        s = Session(title=DEFAULT_TITLE)
        s.add_message("user", "pool temperature trend")
        assert s.title == "Pool Temperature Trend"
