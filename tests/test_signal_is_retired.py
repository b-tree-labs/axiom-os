# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-265 D9: "signal" is retired, and no file says it more often than it did.

"Signal" named three unrelated things (a reading in silver and gold, a statement
the signals extension extracted, and a log record forwarded to the bus), so it
named none of them. The replacements are in the terminology ledger: observation,
reading, statement, event. The renames land owner by owner, expand-first, so the
word cannot disappear at once. This guard keeps it from spreading meanwhile: every
file under ``src/`` has a recorded count, a file may only go down, and a new file
may not use the word at all.

The operating-system meaning is not this word and is not counted: ``import
signal``, ``signal.SIGTERM``, ``signal.signal(...)``, a signal handler. The rename
is by sense, never by substitution (the ``stream`` -> ``feed`` precedent).

When a rename lowers a count, lower it in the debt list in the same change, so the
progress cannot be undone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
DEBT = Path(__file__).with_name("signal_term_debt.json")

_WORD = re.compile(r"signals?", re.IGNORECASE)
#: The operating system's signals: not the retired term.
_OS = re.compile(
    r"import signal\b|from signal import|\bsignal\.(SIG\w+|signal|Signals|alarm|pause|setitimer|"
    r"getsignal|strsignal|valid_signals|raise_signal|pthread_kill|default_int_handler|Handlers)\b|"
    r"\bSIG[A-Z]{2,}\b|signal[ _-]?handler|os\.kill|send_signal|\bsignum\b|_signal\b|signal_name",
)


#: Names already scheduled for a mechanical rename (ADR-265 step 1), so code that must
#: say them until then is not blocked: the tables, and the extension's import path.
_SCHEDULED = re.compile(r"\b(silver|gold|bronze)\.signals\b|builtins[./]signals\b|signals_ext\b|axiom_signals__\w+|signal_event_registry\b")
#: The extension renamed as a whole in step 3; its own files are not counted.
_RENAMED_WHOLE = ("src/axiom/extensions/builtins/signals/",)


def _count(text: str) -> int:
    n = 0
    for line in text.splitlines():
        if not _WORD.search(line) or _OS.search(line):
            continue
        n += len(_WORD.findall(_SCHEDULED.sub("", line)))
    return n


def _counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for path in sorted(SRC.rglob("*")):
        if path.suffix not in (".py", ".toml", ".md", ".sql") or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO).as_posix()
        if "tests" in path.relative_to(SRC).parts or rel.startswith(_RENAMED_WHOLE):
            continue
        n = _count(path.read_text(encoding="utf-8", errors="replace"))
        if n:
            out[rel] = n
    return out


def test_the_operating_system_meaning_is_not_counted():
    assert _count("import signal\nsignal.signal(signal.SIGTERM, handler)\n") == 0
    assert _count("os.kill(pid, signal.SIGTERM)  # a signal handler") == 0
    assert _count("class SignalSink:\nsignal_event = 'x'\n") == 2


def test_names_already_scheduled_for_rename_are_not_counted():
    assert _count("SELECT * FROM silver.signals JOIN gold.signals") == 0
    assert _count("from axiom.extensions.builtins.signals import briefing") == 0
    # The bus registry's old filename, still read for one release by the forwarder's rename.
    assert _count("signal_event_registry.toml is still read") == 0


def test_no_file_says_signal_more_than_it_did():
    debt: dict[str, int] = json.loads(DEBT.read_text(encoding="utf-8"))
    now = _counts()
    worse = {p: (debt.get(p, 0), n) for p, n in now.items() if n > debt.get(p, 0)}
    assert not worse, (
        '"signal" is retired (ADR-265 D9; docs/conventions/terminology-ledger.md). Use '
        "observation, reading, statement or event, or the name of what it is. These files "
        f"say it more than the debt list allows (allowed, now): {worse}"
    )


def test_the_debt_list_names_only_files_that_still_owe():
    """A file that no longer says the word leaves the list, so the list only shrinks."""
    debt: dict[str, int] = json.loads(DEBT.read_text(encoding="utf-8"))
    now = _counts()
    gone = sorted(p for p in debt if p not in now)
    assert not gone, f"remove these from {DEBT.name}; they no longer say the word: {gone}"
