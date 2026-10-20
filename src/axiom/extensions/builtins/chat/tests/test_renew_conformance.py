# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Cross-harness conformance: the same properties asserted of EVERY session reader that ships.

Parametrized harness x property, run against golden fixtures captured from real installs (see
``fixtures/sessions/*/manifest.json``; Claude Code and Codex use the in-repo builders), so it needs no harness
installed. Each property is one that /renew promises regardless of which harness a session came from:
read-only sources, secrets withheld, idempotent import, tier hold, loud and bounded degradation.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import sys

import pytest

from axiom.extensions.builtins.chat.commands_ext import renew, renew_sources
from axiom.infra.orchestrator.session import Session
from axiom.memory.absorb import sessions as sessions_mod
from axiom.memory.absorb import sessions_files
from axiom.memory.absorb.base import SessionCapable
from axiom.memory.absorb.sessions import session_readers

from ._renew_harness_support import SECRET, fingerprint
from ._renew_support import ME, Agent, FakeGateway, new_store
from ._session_conformance_support import Case, all_cases
from .test_renew_import import _providers

CASES = all_cases()
FILE_CASES = [c for c in CASES if c.kind == "file"]
DB_CASES = [c for c in CASES if c.kind == "db"]
JSONL_CASES = [c for c in FILE_CASES if c.jsonl]
DEGRADABLE = [c for c in CASES if c.degrade is not None]
IDS = lambda cases: [c.name for c in cases]  # noqa: E731

TOOL_LINE = re.compile(r"^\[tool (call|result):", re.MULTILINE)


@pytest.fixture
def home(_no_foreign_harness_state):
    return _no_foreign_harness_state


@pytest.fixture
def rig(tmp_path, home, monkeypatch):
    monkeypatch.setattr("axiom.infra.orchestrator.session._default_principal", lambda: ME)
    monkeypatch.setattr("axiom.infra.orchestrator.session.may_adopt", lambda p: False)
    return new_store(tmp_path), Agent(Session(principal_id=ME)), home


def reader_for(case: Case, home):
    return session_readers(home)[case.name]


_INSTALLED: set = set()


def _ensure_installed(case: Case, home) -> None:
    """Install the golden session once per home (a second install would restamp the source's mtime)."""
    if not case.primary(home).exists():
        case.install(home)


def _import(rig, case: Case, source_id: str | None = None) -> str:
    store, agent, home = rig
    _ensure_installed(case, home)
    return renew.resume_session(store, agent, f"{case.name}:{source_id or case.session_id}")


def _tool_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("[tool ")]


# ---- lists, loads, imports ----------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_lists_the_session_with_a_real_header(case, home):
    case.install(home)
    reader = reader_for(case, home)
    assert isinstance(reader, SessionCapable) and reader.harness == case.name
    headers = reader.list_sessions()
    mine = [h for h in headers if h.source_session_id == case.session_id]
    assert len(mine) == 1, [h.source_session_id for h in headers]
    h = mine[0]
    assert h.harness == case.name and h.message_count >= 2 and h.updated_at and h.title
    assert h.format_version == case.expected_format_version
    assert reader.skipped == []


@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_load_keeps_roles_and_keeps_tool_traffic_as_text_without_secrets(case, home):
    case.install(home)
    t = reader_for(case, home).load_session(case.session_id)
    assert t is not None and t.messages[0].role == "user"
    assert {m.role for m in t.messages} <= {"user", "assistant"}
    assert t.header.message_count == len(t.messages)
    assistant = "\n".join(m.content for m in t.messages if m.role == "assistant")
    lines = _tool_lines(assistant)
    # tool traffic is TEXT, counted: calls/results are summarised, never copied
    assert len([ln for ln in lines if ln.startswith("[tool call:")]) == t.lossy.get("tool_calls", 0)
    assert len([ln for ln in lines if ln.startswith("[tool result:")]) == t.lossy.get(
        "tool_results", 0
    )
    assert all(
        re.fullmatch(r"\[tool result: \d+ chars, not imported\]", ln)
        for ln in lines
        if "result" in ln[:14]
    )
    for ln in lines:
        assert SECRET not in ln  # the args hint withholds it; a result's content is never copied
    assert any(ln.startswith("[tool call:") for ln in lines), "the fixture exercises a tool call"
    assert any("(withheld)" in ln for ln in lines), (
        "a secret in tool arguments is replaced, not shown"
    )


@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_import_makes_a_held_native_session_with_provenance_and_the_format_version(case, rig):
    store, agent, _ = rig
    out = _import(rig, case)
    assert "renewed" in out and "imported, source unchanged" in out
    prov = agent.session.context["renewed_from"]
    assert prov["harness"] == case.name and prov["source_session_id"] == case.session_id
    assert prov["format_version"] == case.expected_format_version  # recorded with the origin
    assert prov["tier"] == "unclassified" and prov["tier_confirmed"] is False
    assert prov["message_count"] == len(agent.session.messages)
    assert renew_sources.tier_held(agent.session)
    assert agent.session.messages[0].role == "user"


@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_every_import_is_held_back_from_cloud_providers_until_confirmed(case, rig, tmp_path):
    store, agent, _ = rig
    _import(rig, case)
    refusal = renew_sources.send_refusal(agent.session, FakeGateway(*_providers(tmp_path)))
    assert "Not sent" in refusal and "cloud-llm" in refusal
    renew_sources.confirm_tier(agent, store)
    assert renew_sources.send_refusal(agent.session, FakeGateway(*_providers(tmp_path))) == ""


@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_reimport_is_idempotent_and_keeps_what_was_said_after(case, rig):
    store, agent, _ = rig
    _import(rig, case)
    first, n = agent.session.session_id, len(agent.session.messages)
    agent.session.add_message("user", "my follow-up")
    store.save(agent.session)
    other = Agent(Session(principal_id=ME))
    out = renew.resume_session(store, other, f"{case.name}:{case.session_id}")
    natives = [
        s
        for s in store.list_sessions()
        if "renewed_from" in (store.peek(s) or {}).get("context", {})
    ]
    assert other.session.session_id == first and natives == [first]
    assert (
        len(other.session.messages) == n + 1
        and other.session.messages[-1].content == "my follow-up"
    )
    assert "updated the earlier import" in out


@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_the_source_is_byte_and_mtime_identical_after_listing_and_importing(case, rig):
    _, _, home = rig
    _ensure_installed(case, home)
    before = fingerprint(home)
    reader = reader_for(case, home)
    reader.list_sessions()
    reader.load_session(case.session_id)
    _import(rig, case)
    _import(rig, case)
    assert fingerprint(home) == before


# ---- secrets ------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_a_title_that_reads_as_a_secret_is_withheld_everywhere_it_could_surface(
    case, rig, tmp_path
):
    store, agent, home = rig
    _ensure_installed(case, home)
    case.secret_title(home)
    rows = renew_sources.foreign_rows(ME, case.name)
    mine = next(r for r in rows if r["ref"] == case.session_id)
    assert SECRET not in mine["title"] and "withheld" in mine["title"]
    assert SECRET not in "\n".join(renew.render_rows(rows))
    assert SECRET not in renew.list_json(store, Session(principal_id=ME))
    _import(rig, case)
    assert SECRET not in agent.session.title
    # the on-disk header cache holds titles, never a secret
    cache = os.environ["AXI_STATE_DIR"] + "/renew-session-index.json"
    if os.path.exists(cache):
        assert SECRET not in open(cache, encoding="utf-8").read()


# ---- unknown or newer formats degrade loudly ------------------------------------------------------


@pytest.mark.parametrize("case", DEGRADABLE, ids=IDS(DEGRADABLE))
def test_an_unsupported_format_is_refused_with_a_named_reason_and_nothing_partial_is_imported(
    case, rig
):
    store, agent, home = rig
    case.install(home)
    bad = case.degrade(home, case)
    reader = reader_for(case, home)
    headers = reader.list_sessions()
    assert bad not in {h.source_session_id for h in headers}
    assert reader.skipped and all(s.reason for s in reader.skipped)
    assert any("unsupported format" in s.reason for s in reader.skipped), [
        s.reason for s in reader.skipped
    ]
    if case.degrade_reason:
        assert any(case.degrade_reason in s.reason for s in reader.skipped)
    if case.kind == "file":  # the rest of the harness keeps listing
        assert case.session_id in {h.source_session_id for h in headers}
    assert reader.load_session(bad) is None and "unsupported format" in reader.last_error
    sid, why = renew_sources.import_session(store, agent, case.name, bad)
    assert sid == "" and "unsupported format" in why
    assert not [
        s
        for s in store.list_sessions()
        if "renewed_from" in (store.peek(s) or {}).get("context", {})
    ]


# ---- corrupt, truncated, empty, garbage ----------------------------------------------------------


GARBAGE = {
    "empty": b"",
    "garbage": b"\x00\x01not json at all\xff\xfe" * 20,
    "truncated": None,  # first half of the real file
    "binary-lines": b'{"a":\n{"b"\n]]]\n',
}


@pytest.mark.parametrize("case", FILE_CASES, ids=IDS(FILE_CASES))
@pytest.mark.parametrize("flavor", sorted(GARBAGE))
def test_a_damaged_session_file_is_skipped_with_its_name_and_the_rest_keep_listing(
    case, flavor, home
):
    case.install(home)
    bad_id = case.fresh_ids[0]
    target = case.clone(home, bad_id)
    data = GARBAGE[flavor]
    if data is None:
        whole = target.read_bytes()
        data = whole[: len(whole) // 2]
    target.write_bytes(data)
    reader = reader_for(case, home)
    headers = reader.list_sessions()
    got = {h.source_session_id for h in headers}
    assert case.session_id in got, "the good session still lists"
    if bad_id in got:
        # a half file can still hold a valid prefix of the conversation: that is a consistent prefix, not garbage
        assert flavor == "truncated"
    else:
        assert any(target.name in s.source or bad_id in s.source for s in reader.skipped), (
            reader.skipped
        )
        assert all(s.reason for s in reader.skipped)
    assert reader.load_session(case.session_id) is not None


@pytest.mark.parametrize("case", DB_CASES, ids=IDS(DB_CASES))
@pytest.mark.parametrize("flavor", ["empty", "garbage", "truncated"])
def test_a_damaged_database_lists_nothing_and_says_why(case, flavor, home, rig):
    case.install(home)
    db = home / case.store_rel
    whole = db.read_bytes()
    db.write_bytes(
        {"empty": b"", "garbage": b"definitely not a database" * 50, "truncated": whole[:200]}[
            flavor
        ]
    )
    reader = reader_for(case, home)
    assert reader.list_sessions() == []
    assert reader.skipped and reader.skipped[0].reason  # named, not silent
    assert reader.load_session(case.session_id) is None and reader.last_error
    sid, why = renew_sources.import_session(rig[0], rig[1], case.name, case.session_id)
    assert sid == "" and "not found" in why


# ---- a harness appending while we read ------------------------------------------------------------


@pytest.mark.parametrize("case", JSONL_CASES, ids=IDS(JSONL_CASES))
def test_a_half_written_last_record_is_a_consistent_prefix_not_a_crash(case, home):
    case.install(home)
    reader = reader_for(case, home)
    base = reader.load_session(case.session_id)
    with open(case.primary(home), "ab") as fh:
        fh.write(
            b'{"type": "message", "id": "late", "message": {"role": "user", "content": [{"type": "te'
        )
    grown = reader_for(case, home).load_session(case.session_id)
    assert grown is not None
    assert [(m.role, m.content) for m in grown.messages] == [
        (m.role, m.content) for m in base.messages
    ]
    assert grown.lossy.get("partial_trailing_line", 0) == 1


@pytest.mark.parametrize("case", DB_CASES, ids=IDS(DB_CASES))
def test_an_uncommitted_write_in_flight_is_invisible_to_the_reader(case, home):
    case.install(home)
    reader = reader_for(case, home)
    base = reader.load_session(case.session_id)
    writer = sqlite3.connect(home / case.store_rel, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    tables = [r[0] for r in writer.execute("select name from sqlite_master where type='table'")]
    # an in-flight transaction that rewrites every text row; the reader must see the committed state
    for t in tables:
        for _, col, typ, *_ in writer.execute(f"pragma table_info({t})").fetchall():  # noqa: S608
            pass
    writer.execute("DELETE FROM " + [t for t in tables if t in ("message", "messages")][0])
    try:
        again = reader_for(case, home).load_session(case.session_id)
        assert again is not None and [m.content for m in again.messages] == [
            m.content for m in base.messages
        ]
    finally:
        writer.execute("ROLLBACK")
        writer.close()


# ---- big sessions: bounded, stated, never silent -----------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_a_message_cap_keeps_the_newest_and_says_how_many_were_left_out(case, rig, monkeypatch):
    store, agent, home = rig
    case.install(home)
    full = reader_for(case, home).load_session(case.session_id)
    monkeypatch.setattr(sessions_mod, "MAX_MESSAGES", 1)
    capped = reader_for(case, home).load_session(case.session_id)
    assert len(capped.messages) == 1 and capped.messages[0].content == full.messages[-1].content
    assert capped.lossy["older_messages_omitted"] == len(full.messages) - 1
    out = _import(rig, case)
    assert "older messages left out" in out  # shown to the person, not silent


@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_a_very_long_message_is_cut_at_the_cap_and_counted(case, home, monkeypatch):
    case.install(home)
    monkeypatch.setattr(sessions_mod, "MAX_MESSAGE_CHARS", 30)
    t = reader_for(case, home).load_session(case.session_id)
    assert all(len(m.content) < 30 + 80 for m in t.messages)
    assert t.lossy["long_messages_truncated"] >= 1
    assert any("cut: message longer than the import cap" in m.content for m in t.messages)


@pytest.mark.parametrize("case", FILE_CASES, ids=IDS(FILE_CASES))
def test_a_file_over_the_size_cap_is_refused_not_truncated(case, home, monkeypatch):
    case.install(home)
    monkeypatch.setattr(sessions_mod, "MAX_FILE_BYTES", 10)
    monkeypatch.setattr(sessions_files, "MAX_DOC_BYTES", 10)
    reader = reader_for(case, home)
    assert reader.list_sessions() == []
    assert any("import cap" in s.reason for s in reader.skipped)
    assert reader.load_session(case.session_id) is None and "import cap" in reader.last_error


@pytest.mark.parametrize("case", JSONL_CASES, ids=IDS(JSONL_CASES))
def test_a_very_long_line_is_skipped_without_buffering_it_and_the_rest_imports(
    case, home, monkeypatch
):
    case.install(home)
    base = reader_for(case, home).load_session(case.session_id)
    monkeypatch.setattr(sessions_mod, "MAX_LINE_BYTES", 2000)
    with open(case.primary(home), "ab") as fh:
        fh.write(b"\n" + b'{"pad": "' + b"x" * 50_000 + b'"}\n')
    t = reader_for(case, home).load_session(case.session_id)
    assert [(m.role, m.content) for m in t.messages] == [(m.role, m.content) for m in base.messages]
    assert t.lossy.get("oversize_lines_skipped", 0) == 1


# ---- missing, renamed, unreadable stores --------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_a_missing_harness_directory_lists_nothing_and_explains(case, home):
    reader = reader_for(case, home)
    assert reader.list_sessions() == []
    assert reader.notes and case.name in reader.notes[0] and "does not exist" in reader.notes[0]
    assert reader.load_session(case.session_id) is None
    assert renew_sources.explain_empty(case.name).strip()


@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_a_renamed_store_is_just_missing(case, home):
    case.install(home)
    root = home / case.store_root_rel
    root.rename(root.with_name(root.name + ".moved"))
    reader = reader_for(case, home)
    assert reader.list_sessions() == [] and reader.notes


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX permissions and a non-root user",
)
@pytest.mark.parametrize("case", CASES, ids=IDS(CASES))
def test_a_permission_denied_store_lists_nothing_and_does_not_crash(case, home):
    case.install(home)
    root = home / case.store_root_rel
    root.chmod(0)
    try:
        reader = reader_for(case, home)
        assert reader.list_sessions() == []
        assert reader.notes or reader.skipped
        assert reader.load_session(case.session_id) is None
    finally:
        root.chmod(stat.S_IRWXU)


@pytest.mark.parametrize("case", FILE_CASES, ids=IDS(FILE_CASES))
def test_a_symlink_loop_in_the_store_cannot_hang_or_duplicate_the_listing(case, home):
    case.install(home)
    root = home / case.store_root_rel
    (root / "loop").symlink_to(root)
    # and a directory alias at the depth where sessions live, so a reader that follows links would list twice
    store_dir = case.primary(home).parent.parent
    if store_dir != root and store_dir.parent.exists():
        (store_dir.parent / "alias").symlink_to(store_dir)
    ids = [h.source_session_id for h in reader_for(case, home).list_sessions()]
    assert ids.count(case.session_id) == 1


@pytest.mark.parametrize("case", FILE_CASES, ids=IDS(FILE_CASES))
def test_a_symlinked_session_file_is_not_followed(case, home, tmp_path):
    case.install(home)
    target = case.clone(home, case.fresh_ids[0])
    outside = tmp_path / "elsewhere.dat"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(outside)
    ids = {h.source_session_id for h in reader_for(case, home).list_sessions()}
    assert case.fresh_ids[0] not in ids and case.session_id in ids


@pytest.mark.parametrize("case", DB_CASES, ids=IDS(DB_CASES))
def test_a_database_that_is_a_symlink_loop_lists_nothing(case, home):
    case.install(home)
    db = home / case.store_rel
    db.unlink()
    db.symlink_to(db)
    reader = reader_for(case, home)
    assert reader.list_sessions() == []


# ---- bytes ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", FILE_CASES, ids=IDS(FILE_CASES))
def test_non_utf8_bytes_degrade_to_replacement_characters(case, home):
    case.install(home)
    p = case.primary(home)
    raw = p.read_bytes()
    word = case.probe.encode()
    assert word in raw
    p.write_bytes(raw.replace(word, word[:1] + b"\xff" + word[2:]))
    t = reader_for(case, home).load_session(case.session_id)
    assert t is not None and case.probe[:1] + "\ufffd" + case.probe[2:] in "\n".join(
        m.content for m in t.messages
    )


@pytest.mark.parametrize("case", DB_CASES, ids=IDS(DB_CASES))
def test_non_utf8_bytes_in_a_database_degrade_to_replacement_characters(case, home):
    case.install(home)
    con = sqlite3.connect(home / case.store_rel)
    for (table,) in con.execute("select name from sqlite_master where type='table'").fetchall():
        for _, col, typ, *_ in con.execute(f"pragma table_info({table})").fetchall():  # noqa: S608
            if typ.upper().startswith("TEXT"):
                con.execute(  # noqa: S608
                    f"UPDATE {table} SET {col} = replace({col}, 'pong', CAST(X'70ff6e67' AS TEXT)) WHERE {col} LIKE '%pong%'"
                )
    con.commit()
    con.close()
    t = reader_for(case, home).load_session(case.session_id)
    assert t is not None and "p\ufffdng" in "\n".join(m.content for m in t.messages)


# ---- identity and order ---------------------------------------------------------------------------


DUP_CASES = [
    c for c in FILE_CASES if c.name in ("claude-code", "pi", "continue", "cursor", "cline")
]


def test_the_same_session_id_in_two_harnesses_stays_two_sessions(rig):
    store, agent, home = rig
    shared = "00000000-0000-4000-8000-00000000d00d"
    for c in DUP_CASES:
        c.install(home)
        c.clone(home, shared)
    rows = renew_sources.foreign_rows(ME, None, limit=50)
    dups = [r for r in rows if r["ref"] == shared]
    assert sorted(r["source"] for r in dups) == sorted(c.name for c in DUP_CASES)
    assert len({r["id"] for r in dups}) == len(DUP_CASES)  # distinct row ids: "<harness>:<id>"
    for c in DUP_CASES:
        a = Agent(Session(principal_id=ME))
        renew.resume_session(store, a, f"{c.name}:{shared}")
        assert a.session.context["renewed_from"]["harness"] == c.name
    natives = [
        s
        for s in store.list_sessions()
        if "renewed_from" in (store.peek(s) or {}).get("context", {})
    ]
    assert len(natives) == len(DUP_CASES)


@pytest.mark.parametrize("case", FILE_CASES, ids=IDS(FILE_CASES))
def test_equal_or_skewed_clocks_order_deterministically_newest_first(case, home):
    case.install(home)
    a, b = case.fresh_ids
    pa, pb = case.clone(home, a), case.clone(home, b)
    stamp = 1_700_000_000
    for p in (pa, pb, case.primary(home)):
        os.utime(
            p, (stamp, stamp)
        )  # identical mtimes: the tie breaks on source id, not on directory order
    first = [h.source_session_id for h in reader_for(case, home).list_sessions()]
    assert first == [h.source_session_id for h in reader_for(case, home).list_sessions()]
    if case.stamp_from_content:
        return  # this harness stamps its own sessions inside the file; mtime is not the ordering key
    # a session stamped in the future is "newest", one with a long-ago stamp is oldest
    os.utime(pb, (stamp + 10**8, stamp + 10**8))
    os.utime(pa, (stamp - 10**8, stamp - 10**8))
    ordered = [h.source_session_id for h in reader_for(case, home).list_sessions()]
    assert ordered.index(b) < ordered.index(case.session_id) < ordered.index(a)


def test_a_missing_or_unparsable_timestamp_sorts_oldest_with_ties_broken_by_id():
    rows = [
        {"id": "x:b", "updated_at": "garbage"},
        {"id": "x:a", "updated_at": ""},
        {"id": "x:c", "updated_at": "2026-10-01T00:00:00+00:00"},
        {"id": "x:d", "updated_at": "2026-10-02T00:00:00Z"},
    ]
    assert [r["id"] for r in renew_sources.merge_newest_first(rows, 10)] == [
        "x:d",
        "x:c",
        "x:a",
        "x:b",
    ]
