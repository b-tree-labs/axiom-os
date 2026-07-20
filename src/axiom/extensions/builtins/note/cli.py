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

def cmd_note(args: argparse.Namespace) -> None:
    note_file = _today_file()

    if args.list:
        # Show recent note files
        notes_dir = _notes_dir()
        files = sorted(notes_dir.glob("*.md"), reverse=True)[:10]
        if not files:
            print("No notes yet. Try: axi note \"your thought here\"")
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
        prog="axi note",
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
    args = parser.parse_args(argv)
    cmd_note(args)


if __name__ == "__main__":
    main()
