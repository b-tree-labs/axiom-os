# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Cross-harness conformance support: one ``Case`` per session reader, built from the golden fixtures captured
from real installs (``tests/fixtures/sessions/<harness>-<version>/``) so the suite runs with no harness installed.

The fixtures carry ``@@SECRET@@`` where the capture used a fake credential; it is replaced here with the test
suite's own secret-shaped string (built from pieces so no scanner trips on the repo). SQLite stores are rebuilt
from ``store.sql`` (the real CREATE TABLE statements and rows) into a rollback-journal database, so a byte-level
"source untouched" check is meaningful.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ._renew_harness_support import CLAUDE_ID, CODEX_ID, SECRET, claude_session, codex_session

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"
HOURS = 3600.0


def _subst(text: str) -> str:
    return text.replace("@@SECRET@@", SECRET)


def fixture_dir(harness: str) -> Path:
    found = sorted(d for d in FIXTURES.iterdir() if d.is_dir() and (d / "manifest.json").exists())
    for d in found:
        if json.loads((d / "manifest.json").read_text())["harness"] == harness:
            return d
    raise KeyError(harness)


def fixture_manifest(harness: str) -> dict:
    return json.loads((fixture_dir(harness) / "manifest.json").read_text())


def install_fixture(harness: str, home: Path) -> dict:
    """Put the golden session for ``harness`` under ``home`` exactly where the harness keeps it."""
    d = fixture_dir(harness)
    m = fixture_manifest(harness)
    for src, dest in (m.get("files") or {}).items():
        target = home / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_subst((d / src).read_text(encoding="utf-8")), encoding="utf-8")
    for src, dest in (m.get("sql") or {}).items():
        target = home / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(target)
        con.executescript(_subst((d / src).read_text(encoding="utf-8")))
        con.commit()
        con.close()
    return m


def copy_text(src: Path, dst: Path, mutate: Callable[[str], str] = lambda t: t) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(mutate(src.read_text(encoding="utf-8")), encoding="utf-8")
    return dst


@dataclass
class Case:
    name: str  # the reader's harness name
    kind: str  # "file" | "db"
    session_id: str  # the golden session's source id
    install: Callable[[Path], None]
    #: file kinds: clone the golden session under ``new_id``; returns the PRIMARY file (the one a test corrupts)
    clone: Callable[[Path, str], Path] | None = None
    #: a new id that is valid for this harness's id shape
    fresh_ids: tuple[str, ...] = (
        "00000000-0000-4000-8000-0000000000a1",
        "00000000-0000-4000-8000-0000000000a2",
    )
    #: make a (clone / the store) unreadable by FORMAT VERSION or STRUCTURE; returns the id that must be refused,
    #: or None when the harness's format carries nothing to refuse on
    degrade: Callable[[Path, Case], str] | None = None
    degrade_reason: str = ""
    #: make the golden session's harness-side TITLE contain the secret
    secret_title: Callable[[Path], None] = lambda home: None
    expected_format_version: str = ""
    jsonl: bool = False
    #: ordering comes from a stamp inside the file/db (not the file mtime)
    stamp_from_content: bool = False
    probe: str = "pong"  # a word in the golden conversation, used to inject bad bytes
    store_rel: str = ""  # DB kinds: the database path under home
    store_root_rel: str = ""  # directory whose absence/unreadability means "no sessions"
    extra: dict = field(default_factory=dict)

    def primary(self, home: Path) -> Path:
        """The golden session's primary file (file kinds) or the database (db kinds)."""
        if self.kind == "db":
            return home / self.store_rel
        return self.clone_source(home)

    def clone_source(self, home: Path) -> Path:
        return self.extra["primary"](home)


def _db_exec(home: Path, case: Case, sql: str, *args) -> None:
    con = sqlite3.connect(home / case.store_rel)
    con.execute(sql, args)
    con.commit()
    con.close()


def _manifest_primary(harness: str, which: int = 0) -> Callable[[Path], Path]:
    def find(home: Path) -> Path:
        m = fixture_manifest(harness)
        dests = [
            d for d in m["files"].values() if not d.endswith(("projects.json", "/sessions.json"))
        ]
        return home / sorted(dests)[which]

    return find


def _clone_by_replace(
    harness: str,
    which: int,
    old_to_new: Callable[[str, str], list[tuple[str, str]]],
    primary_suffix: str = "",
):
    """Clone every manifest file, renaming path fragments and rewriting ids inside the content."""

    def clone(home: Path, new_id: str) -> Path:
        m = fixture_manifest(harness)
        d = fixture_dir(harness)
        primary = None
        for i, (src, dest) in enumerate(m["files"].items()):
            pairs = old_to_new(m["session_id"], new_id)
            new_dest = dest
            for old, new in pairs:
                new_dest = new_dest.replace(old, new)
            target = home / new_dest
            text = _subst((d / src).read_text(encoding="utf-8"))
            for old, new in pairs:
                text = text.replace(old, new)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            if (primary_suffix and new_dest.endswith(primary_suffix)) or (
                not primary_suffix and i == which
            ):
                primary = target
        assert primary is not None
        return primary

    return clone


# ---- per-harness cases ------------------------------------------------------------------------------


def _claude_case() -> Case:
    def install(home: Path) -> None:
        claude_session(home, CLAUDE_ID, hours_ago=2, extra_tool_arg=f"echo {SECRET}")

    def clone(home: Path, new_id: str) -> Path:
        return claude_session(home, new_id, hours_ago=2.5)

    def secret_title(home: Path) -> None:
        claude_session(
            home, CLAUDE_ID, hours_ago=2, title=f"key {SECRET}", extra_tool_arg=f"echo {SECRET}"
        )

    return Case(
        "claude-code",
        "file",
        CLAUDE_ID,
        install,
        clone,
        secret_title=secret_title,
        jsonl=True,
        extra={
            "primary": lambda home: (
                home / ".claude" / "projects" / "-work-detector" / f"{CLAUDE_ID}.jsonl"
            )
        },
        store_root_rel=".claude/projects",
        expected_format_version="2.1",
        probe="files",
    )


def _codex_case() -> Case:
    def install(home: Path) -> None:
        codex_session(home, CODEX_ID, hours_ago=1, command=f"echo {SECRET}")

    def clone(home: Path, new_id: str) -> Path:
        return codex_session(home, new_id, hours_ago=1.5)

    def secret_title(home: Path) -> None:
        codex_session(
            home, CODEX_ID, hours_ago=1, prompt=f"use {SECRET} please", command=f"echo {SECRET}"
        )

    return Case(
        "codex",
        "file",
        CODEX_ID,
        install,
        clone,
        secret_title=secret_title,
        jsonl=True,
        fresh_ids=("019e04f7-0000-7790-b5f3-0000000000a1", "019e04f7-0000-7790-b5f3-0000000000a2"),
        extra={
            "primary": lambda home: next(
                (home / ".codex" / "sessions").rglob(f"rollout-*{CODEX_ID}.jsonl"), home / "absent"
            )
        },
        store_root_rel=".codex",
        expected_format_version="0.129.0",
        probe="flat",
    )


def _db_case(
    harness: str, store_rel: str, title_sql: str, break_sql: str, expected_version: str
) -> Case:
    m = fixture_manifest(harness)
    case = Case(
        harness,
        "db",
        m["session_id"],
        lambda home: install_fixture(harness, home),
        store_rel=store_rel,
        store_root_rel=str(Path(store_rel).parent),
        expected_format_version=expected_version,
    )

    def secret_title(home: Path) -> None:
        _db_exec(home, case, title_sql, SECRET)

    def degrade(home: Path, c: Case) -> str:
        _db_exec(home, c, break_sql)
        return c.session_id

    case.secret_title = secret_title
    case.degrade = degrade
    return case


def _gemini_case() -> Case:
    m = fixture_manifest("gemini")
    sid = m["session_id"]

    def clone(home: Path, new_id: str) -> Path:
        assert len(new_id) == 8
        return _clone_by_replace(
            "gemini", 0, lambda old, new: [(old, new)], primary_suffix=".jsonl"
        )(home, new_id)

    def secret_title(home: Path) -> None:
        p = _manifest_primary("gemini")(home)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"$set": {"summary": f"about {SECRET}"}}) + "\n")

    def degrade(home: Path, c: Case) -> str:
        bad = home / ".gemini" / "tmp" / "work" / "chats" / "session-2026-10-09T00-00-baadf00d.json"
        bad.write_text(json.dumps({"sessionId": "x", "messages": []}), encoding="utf-8")
        return "baadf00d"

    return Case(
        "gemini",
        "file",
        sid,
        lambda home: install_fixture("gemini", home),
        clone,
        fresh_ids=("c0ffee01", "c0ffee02"),
        degrade=degrade,
        secret_title=secret_title,
        jsonl=True,
        stamp_from_content=True,
        extra={"primary": _manifest_primary("gemini")},
        store_root_rel=".gemini/tmp",
    )


def _pi_case() -> Case:
    m = fixture_manifest("pi")
    sid = m["session_id"]

    def clone(home: Path, new_id: str) -> Path:
        return _clone_by_replace("pi", 0, lambda old, new: [(old, new)])(home, new_id)

    def secret_title(home: Path) -> None:
        p = _manifest_primary("pi")(home)
        with open(p, "a", encoding="utf-8") as fh:
            last = json.loads(p.read_text(encoding="utf-8").splitlines()[-1])
            fh.write(
                json.dumps(
                    {
                        "type": "session_info",
                        "id": "zz",
                        "parentId": last["id"],
                        "name": f"about {SECRET}",
                    }
                )
                + "\n"
            )

    def degrade(home: Path, c: Case) -> str:
        bad_id = c.fresh_ids[1]
        target = clone(home, bad_id)
        lines = target.read_text(encoding="utf-8").splitlines()
        head = json.loads(lines[0])
        head["version"] = 99
        target.write_text("\n".join([json.dumps(head), *lines[1:]]) + "\n", encoding="utf-8")
        return bad_id

    return Case(
        "pi",
        "file",
        sid,
        lambda home: install_fixture("pi", home),
        clone,
        degrade=degrade,
        degrade_reason="newer than the verified",
        secret_title=secret_title,
        jsonl=True,
        expected_format_version="3",
        extra={"primary": _manifest_primary("pi")},
        store_root_rel=".pi/agent/sessions",
    )


def _cline_case() -> Case:
    m = fixture_manifest("cline")
    sid = m["session_id"]

    def clone(home: Path, new_id: str) -> Path:
        return _clone_by_replace("cline", 1, lambda old, new: [(old, new)])(home, new_id)

    def edit_meta(home: Path, fn) -> None:
        p = _manifest_primary("cline", 0)(home)
        data = json.loads(p.read_text(encoding="utf-8"))
        fn(data)
        p.write_text(json.dumps(data), encoding="utf-8")

    def secret_title(home: Path) -> None:
        edit_meta(
            home, lambda d: d.setdefault("metadata", {}).__setitem__("title", f"about {SECRET}")
        )

    def degrade(home: Path, c: Case) -> str:
        bad_id = c.fresh_ids[1]
        target = clone(home, bad_id)
        doc = json.loads(target.read_text(encoding="utf-8"))
        doc["version"] = 99
        target.write_text(json.dumps(doc), encoding="utf-8")
        return bad_id

    return Case(
        "cline",
        "file",
        sid,
        lambda home: install_fixture("cline", home),
        clone,
        degrade=degrade,
        degrade_reason="newer than the verified",
        secret_title=secret_title,
        expected_format_version="1",
        stamp_from_content=True,
        fresh_ids=("1791488999001_aaaaa", "1791488999002_bbbbb"),
        extra={"primary": _manifest_primary("cline", 1)},
        store_root_rel=".cline/data/sessions",
    )


def _continue_case() -> Case:
    m = fixture_manifest("continue")
    sid = m["session_id"]

    def clone(home: Path, new_id: str) -> Path:
        return _clone_by_replace("continue", 0, lambda old, new: [(old, new)])(home, new_id)

    def secret_title(home: Path) -> None:
        p = _manifest_primary("continue", 0)(home)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["title"] = f"about {SECRET}"
        p.write_text(json.dumps(data), encoding="utf-8")

    def degrade(home: Path, c: Case) -> str:
        bad_id = c.fresh_ids[1]
        target = clone(home, bad_id)
        doc = json.loads(target.read_text(encoding="utf-8"))
        doc["turns"] = doc.pop("history")  # a renamed top-level key: a different, unknown shape
        target.write_text(json.dumps(doc), encoding="utf-8")
        return bad_id

    return Case(
        "continue",
        "file",
        sid,
        lambda home: install_fixture("continue", home),
        clone,
        degrade=degrade,
        degrade_reason="no history list",
        secret_title=secret_title,
        extra={"primary": _manifest_primary("continue", 0)},
        store_root_rel=".continue/sessions",
    )


def _cursor_case() -> Case:
    m = fixture_manifest("cursor")
    sid = m["session_id"]

    def clone(home: Path, new_id: str) -> Path:
        return _clone_by_replace("cursor", 0, lambda old, new: [(old, new)])(home, new_id)

    return Case(
        "cursor",
        "file",
        sid,
        lambda home: install_fixture("cursor", home),
        clone,
        jsonl=True,
        extra={"primary": _manifest_primary("cursor")},
        store_root_rel=".cursor/projects",
    )


def all_cases() -> list[Case]:
    return [
        _claude_case(),
        _codex_case(),
        _db_case(
            "opencode",
            ".local/share/opencode/opencode.db",
            "UPDATE session SET title = ?",
            "ALTER TABLE part DROP COLUMN data",
            "1.17.8",
        ),
        _db_case(
            "hermes",
            ".hermes/state.db",
            "UPDATE sessions SET title = ?",
            "ALTER TABLE messages DROP COLUMN role",
            "26",
        ),
        _db_case(
            "goose",
            ".local/share/goose/sessions/sessions.db",
            "UPDATE sessions SET name = ?",
            "ALTER TABLE messages DROP COLUMN content_json",
            "16",
        ),
        _gemini_case(),
        _pi_case(),
        _cline_case(),
        _continue_case(),
        _cursor_case(),
    ]


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True)
