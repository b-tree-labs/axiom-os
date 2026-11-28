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
_SCHEDULED = re.compile(r"\b(silver|gold|bronze)\.signals?(_\w+)?\b|builtins[./]signals\b|signals_ext\b|axiom_signals__\w+|signal_event_registry\b")
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
    # The silver companions are renamed with silver itself (ADR-265 step 1, the silver step).
    assert _count("FROM silver.signal_uncertainty JOIN silver.signal_catalogue") == 0
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


# --- ADR-265 step 1: every writer to the silver signal tables goes through one name ---------
#
# The silver tables are renamed in one migration (silver.signals -> silver.readings and its
# companions). A write that spells the old name literally would keep writing to the OLD table
# after the cutover, or fail on it. So the name lives in one module constant, and the guard
# fails a data-modifying statement (INSERT, UPDATE, DELETE, COPY, MERGE, TRUNCATE) that names a
# silver signal table literally anywhere else under ``src/``. DDL is the cutover migration's
# own business and is not a write of rows; readers are unaffected.
NAMES_MODULE = "src/axiom/extensions/builtins/data_platform/silver_names.py"
_WRITE_TO_SILVER_SIGNAL = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|COPY|MERGE\s+INTO|TRUNCATE(?:\s+TABLE)?)\s+"
    r"(?:ONLY\s+)?silver\.signals?\w*",
    re.IGNORECASE,
)


def _literal_silver_writes(text: str) -> list[str]:
    return [m.group(0) for m in _WRITE_TO_SILVER_SIGNAL.finditer(text)]


def _silver_writers_outside_the_name() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel == NAMES_MODULE or "tests" in path.relative_to(SRC).parts:
            continue
        found = _literal_silver_writes(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            out[rel] = found
    return out


def test_the_write_guard_can_fail():
    """A negative control: a literal write is caught, a write through the name and a read are not."""
    assert _literal_silver_writes('cur.execute("INSERT INTO silver.signals (a) VALUES (1)")')
    assert _literal_silver_writes('"DELETE FROM silver.signal_uncertainty WHERE x"')
    assert _literal_silver_writes("UPDATE silver.signal_uncertainty u SET ts = 1")
    assert _literal_silver_writes("COPY silver.signals FROM STDIN")
    assert not _literal_silver_writes('f"INSERT INTO {SILVER_READINGS} (a) VALUES (1)"')
    assert not _literal_silver_writes("SELECT * FROM silver.signals WHERE x")


def test_every_silver_signal_write_goes_through_the_one_name():
    """One place to flip at the cutover (#2137): no literal silver signal table in a write."""
    stray = _silver_writers_outside_the_name()
    assert not stray, (
        "a write to a silver signal table must use the constants in "
        f"{NAMES_MODULE} (ADR-265 step 1: one name to flip at the cutover), not the literal "
        f"table name. Found: {stray}"
    )


# DDL and catalogue lookups: after the cutover ``silver.signals`` is a read-only view, so DDL that
# names it would alter the view and a catalogue lookup by the old name would find no hypertable
# (and answer "plain table", the wrong conflict key). Both take the name from the same module.
# Index and constraint names (``signals_pkey``) are not table names and are not in scope.
_SILVER = r"silver\.(?:signals|signal_uncertainty|signals_identity_audit)(?!\w)"
_DDL_AND_CATALOGUE = (
    re.compile(rf"\b(?:ON|REFERENCES|LIKE|TABLE|EXISTS)\s+(?:ONLY\s+)?{_SILVER}"),
    re.compile(rf"'{_SILVER}'"),
    re.compile(r"\b(?:table_name|relname|tablename|hypertable_name)\s*=\s*'signals(?:_uncertainty)?'"),
    re.compile(r"""\(\s*["']silver["']\s*,\s*["']signals?\w*["']\s*\)"""),
    re.compile(r"""schema\s*=\s*["']silver["']\s*,\s*table\s*=\s*["']signal"""),
)


def _literal_silver_ddl(text: str) -> list[str]:
    found: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for rx in _DDL_AND_CATALOGUE:
            found += [m.group(0) for m in rx.finditer(line)]
    found += [m.group(0) for m in _DDL_AND_CATALOGUE[4].finditer(" ".join(text.split()))]
    return found


def test_the_ddl_guard_can_fail():
    assert _literal_silver_ddl("ALTER TABLE silver.signals ADD COLUMN x text")
    assert _literal_silver_ddl("CREATE INDEX i ON silver.signals (a)")
    assert _literal_silver_ddl("REFERENCES silver.signals (row_hash)")
    assert _literal_silver_ddl("COMMENT ON TABLE silver.signal_uncertainty IS 'x'")
    assert _literal_silver_ddl("WHERE hypertable_schema = 'silver' AND hypertable_name = 'signals'")
    assert _literal_silver_ddl("SELECT to_regclass('silver.signals')")
    assert _literal_silver_ddl('TimeseriesContract(schema="silver",\n  table="signals")')
    assert not _literal_silver_ddl('f"ALTER TABLE {SILVER_READINGS} ADD COLUMN x text"')
    assert not _literal_silver_ddl("DROP CONSTRAINT IF EXISTS signals_pkey")
    assert not _literal_silver_ddl("ALTER INDEX silver.signals_site_stream_channel_ts RENAME TO x")
    assert not _literal_silver_ddl("done.append('row-level security on silver.signals')")
    assert not _literal_silver_ddl("# ALTER TABLE silver.signals DROP CONSTRAINT")


def test_no_silver_signal_ddl_or_catalogue_lookup_names_the_table_literally():
    stray: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel == NAMES_MODULE or "tests" in path.relative_to(SRC).parts:
            continue
        found = _literal_silver_ddl(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            stray[rel] = found
    assert not stray, (
        f"DDL and catalogue lookups on a silver signal table must use the names in {NAMES_MODULE} "
        f"(ADR-265 step 1), not the literal. Found: { {k: v[:3] for k, v in stray.items()} }"
    )
