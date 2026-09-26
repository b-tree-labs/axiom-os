# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axi note — capture a quick thought to the personal RAG knowledge base.

Usage::

    axi note "meeting with Dr. Smith: agreed CFD mesh stays on internal server"
    axi note                   # open $EDITOR for a longer note
    axi note --list            # show recent daily note files

Notes are appended (with a timestamp) to::

    runtime/knowledge/notes/YYYY-MM-DD.md

and immediately indexed into the personal RAG corpus (rag-internal) in a
background thread — zero impact on the command return time.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _notes_dir() -> Path:
    from axiom import REPO_ROOT
    d = REPO_ROOT / "runtime" / "knowledge" / "notes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _today_file() -> Path:
    return _notes_dir() / f"{datetime.now().strftime('%Y-%m-%d')}.md"


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%H:%M UTC")


def _inbox_dir() -> Path:
    from axiom import REPO_ROOT
    d = REPO_ROOT / "runtime" / "inbox" / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_note(note_file: Path, text: str) -> None:
    """Append *text* to *note_file* with a timestamp header.

    Also drops a copy into runtime/inbox/raw/ so SCAN can ingest it
    as a freetext signal. This bridges knowledge capture → signal pipeline.
    """
    header = f"\n## {_timestamp()}\n\n"
    with note_file.open("a", encoding="utf-8") as f:
        f.write(header + text.strip() + "\n")

    # Copy to signal inbox for SCAN ingestion
    try:
        inbox = _inbox_dir()
        ts = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        inbox_file = inbox / f"note_{ts}.md"
        inbox_file.write_text(f"# Note — {ts}\n\n{text.strip()}\n", encoding="utf-8")
    except Exception:
        pass  # Inbox copy is best-effort


def _index_file_background(note_file: Path) -> None:
    """Re-index *note_file* in a daemon thread — does not block the caller."""
    def _run():
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore
            url = SettingsStore().get("rag.database_url", "")
            if not url:
                return
            from axiom import REPO_ROOT
            from axiom.rag.ingest import ingest_file
            from axiom.rag.store import RAGStore
            store = RAGStore(url)
            store.connect()
            ingest_file(note_file, store, repo_root=REPO_ROOT / "runtime" / "knowledge")
            store.close()
        except Exception:
            pass  # RAG indexing is best-effort

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

#: Words that are almost certainly a subcommand somebody typed, not a note
#: they meant to keep. `axi note list` silently saved a note whose entire
#: content was "list" — a read-shaped verb that wrote to the knowledge base,
#: and left a file behind to be cleaned up by hand.
_LISTING_WORDS = frozenset({"list", "ls", "recent"})


def cmd_note(args: argparse.Namespace) -> None:
    note_file = _today_file()

    # A one-word note matching a listing verb is a mistake, not a thought.
    # Capturing it is the destructive reading, so take the safe one and say
    # how to force the other.
    if (
        not args.list
        and not getattr(args, "literal", False)
        and len(args.text) == 1
        and args.text[0].lower() in _LISTING_WORDS
    ):
        print(
            f"Reading {args.text[0]!r} as a request to list notes, not as a note.\n"
            f"  To capture that word literally:  {_brand_cli()} note -- {args.text[0]}"
        )
        args.list = True

    if args.list:
        # Show recent note files
        notes_dir = _notes_dir()
        files = sorted(notes_dir.glob("*.md"), reverse=True)[:10]
        if not files:
            print(f"No notes yet. Try: {_brand_cli()} note \"your thought here\"")
            return
        for f in files:
            size = f.stat().st_size
            print(f"  {f.name}  ({size} bytes)")
        return

    if args.text:
        # Inline note from CLI args
        text = " ".join(args.text)
        _append_note(note_file, text)
        print(f"Noted → {note_file.relative_to(Path.cwd()) if note_file.is_relative_to(Path.cwd()) else note_file}")
        _index_file_background(note_file)
        return

    # No text given — open $EDITOR
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"

    # Write a temp file with a prompt header
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(f"<!-- Note for {datetime.now().strftime('%Y-%m-%d')} — save and close to capture -->\n\n")
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run([editor, str(tmp_path)])
        if result.returncode != 0:
            print("Editor exited with error — note not saved.", file=sys.stderr)
            return
        content = tmp_path.read_text(encoding="utf-8").strip()
        # Strip the comment header we injected
        lines = [line for line in content.splitlines() if not line.startswith("<!--")]
        text = "\n".join(lines).strip()
        if not text:
            print("Empty note — nothing saved.")
            return
        _append_note(note_file, text)
        print(f"Noted → {note_file}")
        _index_file_background(note_file)
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{_brand_cli()} note",
        description="Capture a quick note to the personal RAG knowledge base.",
    )
    parser.add_argument(
        "text",
        nargs="*",
        help="Note text (omit to open $EDITOR)",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List recent daily note files",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = get_parser()
    raw = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    # argparse consumes `--`, so by the time cmd_note sees the text there is
    # nothing left to distinguish `note list` from `note -- list`. The escape
    # hatch the message advertises has to be read from the raw arguments, or
    # it is an instruction that does not work.
    args.literal = "--" in raw
    cmd_note(args)


if __name__ == "__main__":
    main()
