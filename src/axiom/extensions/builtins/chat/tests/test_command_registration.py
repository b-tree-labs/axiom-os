# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A command that is dispatched must be discoverable.

Ben, after `/alerts` shipped: "how do we ensure new additions get picked up
properly in the future?"

Adding a slash command touches three places — the dispatcher, the help listing,
and tab-completion — and nothing checked they agreed. `/alerts` was wired to all
three only because it was done in one sitting; the next one will not be. A
command that dispatches but is not in `CHAT_META_COMMANDS` is invisible: it does
not appear in `/help` and does not complete, so nobody discovers it and it might
as well not exist.

`CHAT_META_COMMANDS` feeds BOTH help and completion, so registration there is
the single thing to get right, and this test is what makes forgetting it fail.
"""

from __future__ import annotations

import re
from pathlib import Path

from axiom.extensions.builtins.chat.commands import CHAT_META_COMMANDS

_CHAT = Path(__file__).resolve().parents[1]

#: Aliases and legacy spellings kept working but deliberately NOT advertised —
#: help listing every spelling of the same thing is how a help screen becomes
#: unreadable. Declared here so the omission is a decision with a reason
#: attached, which is the difference between an alias and an oversight.
UNADVERTISED = {
    "/quit",       # alias for /exit
    "/archive",    # legacy spelling of /sessions archive
    "/rename",     # legacy spelling of /sessions rename
    # cli.py says it outright: "/signal and /pub are the primary names;
    # /sense and /doc are hidden aliases". Listing every spelling of one thing
    # is how a help screen stops being readable.
    "/sense",      # alias for /signal
    "/doc",        # alias for /pub
    "/publisher",  # alias for /pub
}


def _dispatched() -> set[str]:
    """Every slash command either surface actually handles."""
    found: set[str] = set()
    for name in ("fullscreen.py", "cli.py"):
        source = (_CHAT / name).read_text()
        found |= set(re.findall(r'cmd == "(/[a-z]+)"', source))
        for group in re.findall(r"cmd in \(([^)]*)\)", source):
            found |= set(re.findall(r'"(/[a-z]+)"', group))
    return found


def _declared() -> set[str]:
    """The first word of every advertised command."""
    return {entry.split()[0] for entry in CHAT_META_COMMANDS}


class TestEveryCommandIsDiscoverable:
    def test_the_scan_finds_commands_at_all(self):
        """Guards the guard: a regex that matched nothing would make every
        assertion below vacuously true."""
        assert len(_dispatched()) >= 5, _dispatched()

    def test_nothing_dispatches_without_being_advertised(self):
        orphans = _dispatched() - _declared() - UNADVERTISED
        assert orphans == set(), (
            f"{sorted(orphans)} are handled but appear in neither /help nor "
            "tab-completion. Add them to CHAT_META_COMMANDS, or to "
            "UNADVERTISED with a reason if they are aliases."
        )

    def test_every_unadvertised_command_really_is_dispatched(self):
        """The exception list must not accumulate names nothing handles —
        that is how an allowlist stops describing reality."""
        stale = UNADVERTISED - _dispatched()
        assert stale == set(), f"{sorted(stale)} are excused but never dispatched"

    def test_alerts_specifically_is_registered(self):
        """The command that prompted this. Named explicitly so a refactor that
        drops it fails by name rather than by count."""
        assert "/alerts" in _declared()
        assert "/alerts" in _dispatched()


class TestHelpAndCompletionCannotDrift:
    def test_they_come_from_one_source(self):
        """Two lists would drift the moment someone updates one. Both read
        CHAT_META_COMMANDS, and this pins that they still do."""
        from axiom.extensions.builtins.chat.commands import SLASH_COMMANDS

        assert set(SLASH_COMMANDS) >= set(CHAT_META_COMMANDS)

    def test_every_advertised_command_has_a_description(self):
        """An entry with no description reaches tab-completion as a bare name
        and teaches nobody what it does."""
        missing = [c for c, desc in CHAT_META_COMMANDS.items() if not (desc or "").strip()]
        assert missing == []


class TestTheHelpScreenStaysAligned:
    """The two-column layout padded the left column to a hard-coded 40 while
    entries ran to 52, so long rows shoved the second column sideways."""

    def test_the_second_column_starts_at_one_place(self):
        from axiom.extensions.builtins.chat.commands import cmd_help

        plain = re.sub(r"\x1b\[[0-9;]*m", "", cmd_help())
        starts = set()
        for line in plain.splitlines():
            for marker in ("/sessions", "/resume", "/save", "/alerts", "/permissions"):
                index = line.find(marker)
                # Only the right-hand column: the left one starts near zero.
                if index > 20:
                    starts.add(index)
        assert len(starts) == 1, f"the right column starts at {sorted(starts)}"
