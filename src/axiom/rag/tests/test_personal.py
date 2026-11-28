# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for rag/personal.py — no postgres required.

All store interactions are mocked. Tests cover:
  - Session text extraction (happy path, too short, malformed JSON, content blocks)
  - Signal text extraction (flat JSON, nested, empty, malformed)
  - _flatten_json (dict, list, scalars, depth limit)
  - Git log extraction (subprocess success, failure, timeout)
  - _upsert deduplication logic (skips unchanged, calls store for new)
  - ingest_sessions / ingest_signals / ingest_git_logs directory iteration
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from axiom.rag.personal import (
    _MIN_SESSION_MESSAGES,
    _extract_session_text,
    _extract_signal_text,
    _flatten_json,
    _git_log_text,
    _md5_text,
    _upsert,
    ingest_git_logs,
    ingest_session_file,
    ingest_sessions,
    ingest_signals,
)
from axiom.rag.store import CORPUS_INTERNAL

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(existing_checksum: str | None = None) -> MagicMock:
    store = MagicMock()
    if existing_checksum is None:
        store.get_document.return_value = None
    else:
        store.get_document.return_value = {"checksum": existing_checksum}
    return store


def _session_json(messages: list[dict], title: str = "Test Session") -> dict:
    return {"title": title, "messages": messages}


def _user_msg(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant_msg(text: str) -> dict:
    return {"role": "assistant", "content": text}


# ---------------------------------------------------------------------------
# _md5_text
# ---------------------------------------------------------------------------

def test_md5_text_is_hex():
    result = _md5_text("hello")
    assert len(result) == 32
    assert all(c in "0123456789abcdef" for c in result)


def test_md5_text_deterministic():
    assert _md5_text("same") == _md5_text("same")
    assert _md5_text("a") != _md5_text("b")


# ---------------------------------------------------------------------------
# _extract_session_text
# ---------------------------------------------------------------------------

def test_extract_session_text_happy_path(tmp_path):
    messages = [_user_msg("hello"), _assistant_msg("hi"), _user_msg("thanks")]
    p = tmp_path / "session.json"
    p.write_text(json.dumps(_session_json(messages)))
    result = _extract_session_text(p)
    assert result is not None
    title, text = result
    assert title == "Test Session"
    assert "**User:** hello" in text
    assert "**Assistant:** hi" in text
    assert "**User:** thanks" in text


def test_extract_session_text_uses_stem_when_no_title(tmp_path):
    messages = [_user_msg("a"), _assistant_msg("b"), _user_msg("c")]
    data = {"messages": messages}  # no title key
    p = tmp_path / "abc123.json"
    p.write_text(json.dumps(data))
    result = _extract_session_text(p)
    assert result is not None
    title, _ = result
    assert "abc123" in title


def test_extract_session_text_too_short_returns_none(tmp_path):
    messages = [_user_msg("hi"), _assistant_msg("hello")]  # only 2 turns
    p = tmp_path / "short.json"
    p.write_text(json.dumps(_session_json(messages)))
    assert _extract_session_text(p) is None


def test_extract_session_text_exactly_min_turns(tmp_path):
    messages = [_user_msg("a"), _assistant_msg("b"), _user_msg("c")]
    assert len(messages) == _MIN_SESSION_MESSAGES
    p = tmp_path / "min.json"
    p.write_text(json.dumps(_session_json(messages)))
    result = _extract_session_text(p)
    assert result is not None


def test_extract_session_text_malformed_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not valid json {{{")
    assert _extract_session_text(p) is None


def test_extract_session_text_missing_file():
    p = Path("/nonexistent/path/session.json")
    assert _extract_session_text(p) is None


def test_extract_session_text_anthropic_content_blocks(tmp_path):
    """Content as list of blocks (Anthropic format) should be joined."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "block one"}, {"type": "text", "text": "block two"}]},
        _assistant_msg("response"),
        _user_msg("follow up"),
    ]
    p = tmp_path / "blocks.json"
    p.write_text(json.dumps(_session_json(messages)))
    result = _extract_session_text(p)
    assert result is not None
    _, text = result
    assert "block one" in text
    assert "block two" in text


def test_extract_session_text_skips_system_messages(tmp_path):
    messages = [
        {"role": "system", "content": "you are helpful"},
        _user_msg("hi"),
        _assistant_msg("hello"),
        _user_msg("bye"),
    ]
    p = tmp_path / "sys.json"
    p.write_text(json.dumps(_session_json(messages)))
    result = _extract_session_text(p)
    assert result is not None
    _, text = result
    assert "you are helpful" not in text


def test_extract_session_text_empty_content_skipped(tmp_path):
    messages = [
        {"role": "user", "content": ""},
        _assistant_msg("hello"),
        _user_msg("ok"),
        _assistant_msg("great"),
    ]
    p = tmp_path / "empty.json"
    p.write_text(json.dumps(_session_json(messages)))
    result = _extract_session_text(p)
    assert result is not None
    _, text = result
    # Empty user message should not appear as a labelled entry
    assert text.count("**User:**") == 1  # only the non-empty one


# ---------------------------------------------------------------------------
# _flatten_json
# ---------------------------------------------------------------------------

def test_flatten_json_simple_dict():
    result = _flatten_json({"key": "value"})
    assert any("key" in line and "value" in line for line in result)


def test_flatten_json_nested():
    result = _flatten_json({"outer": {"inner": "hello"}})
    assert any("inner" in line and "hello" in line for line in result)


def test_flatten_json_list():
    result = _flatten_json({"items": ["a", "b"]})
    assert any("a" in line for line in result)


def test_flatten_json_scalars():
    result = _flatten_json({"count": 42, "flag": True})
    assert any("42" in line for line in result)
    assert any("True" in line for line in result)


def test_flatten_json_depth_limit():
    deep = {"a": {"b": {"c": {"d": {"e": "too deep"}}}}}
    result = _flatten_json(deep, max_depth=2)
    assert not any("too deep" in line for line in result)


def test_flatten_json_empty_strings_skipped():
    result = _flatten_json({"key": ""})
    assert result == []


def test_flatten_json_caps_list_at_50():
    big_list = list(range(100))
    result = _flatten_json({"nums": big_list})
    # At most 50 entries from the list
    assert len(result) <= 50


# ---------------------------------------------------------------------------
# _extract_signal_text
# ---------------------------------------------------------------------------

def test_extract_signal_text_simple(tmp_path):
    data = {"topic": "xenon poisoning", "severity": "high"}
    p = tmp_path / "signal.json"
    p.write_text(json.dumps(data))
    result = _extract_signal_text(p)
    assert result is not None
    title, text = result
    assert "signal" in title.lower()
    assert "xenon" in text


def test_extract_signal_text_empty_json(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("{}")
    assert _extract_signal_text(p) is None


def test_extract_signal_text_malformed(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("!!!not json")
    assert _extract_signal_text(p) is None


def test_extract_signal_text_nested(tmp_path):
    data = {"event": {"type": "alert", "message": "system alert"}}
    p = tmp_path / "nested.json"
    p.write_text(json.dumps(data))
    result = _extract_signal_text(p)
    assert result is not None
    _title, text = result
    assert "system alert" in text


# ---------------------------------------------------------------------------
# _git_log_text
# ---------------------------------------------------------------------------

def test_git_log_text_success(tmp_path):
    fake_output = "### abc123 2026-01-01\nAdd feature\n\nDetails here.\n"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_output)
        result = _git_log_text(tmp_path)
    assert result == fake_output


def test_git_log_text_nonzero_returncode(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=128, stdout="")
        result = _git_log_text(tmp_path)
    assert result is None


def test_git_log_text_empty_output(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="   ")
        result = _git_log_text(tmp_path)
    assert result is None


def test_git_log_text_git_not_found(tmp_path):
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = _git_log_text(tmp_path)
    assert result is None


def test_git_log_text_timeout(tmp_path):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)):
        result = _git_log_text(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# _upsert — deduplication
# ---------------------------------------------------------------------------

def test_upsert_skips_unchanged():
    text = "# Hello\n\nSome content here that is long enough to chunk properly.\n"
    checksum = _md5_text(text)
    store = _make_store(existing_checksum=checksum)

    with patch("axiom.rag.embeddings.embed_texts", return_value=None):
        result = _upsert("sessions/test.json", "Test", "session", text, store, CORPUS_INTERNAL)

    assert result is False
    store.upsert_chunks.assert_not_called()


def test_upsert_indexes_new_content():
    text = "# Hello\n\nThis is new content that should be indexed into the RAG store.\n"
    store = _make_store(existing_checksum=None)

    with patch("axiom.rag.embeddings.embed_texts", return_value=None):
        result = _upsert("sessions/new.json", "New", "session", text, store, CORPUS_INTERNAL)

    assert result is True
    store.upsert_chunks.assert_called_once()


def test_upsert_skips_on_embedding_failure():
    # A configured embedder that fails (e.g. network drop) must NOT commit the doc
    # text-only — that records its checksum and skips it as 'unchanged' forever.
    # It should skip and return False so a re-run retries it.
    from axiom.rag.embeddings import EmbeddingError

    text = "# Hello\n\nContent long enough to chunk into the RAG store on ingest.\n"
    store = _make_store(existing_checksum=None)

    with patch("axiom.rag.personal.embed_texts", side_effect=EmbeddingError("network drop")):
        result = _upsert("sessions/fail.json", "Fail", "session", text, store, CORPUS_INTERNAL)

    assert result is False
    store.upsert_chunks.assert_not_called()


def test_upsert_indexes_when_checksum_changed():
    text = "# Updated content\n\nThis document has changed since last indexing run.\n"
    store = _make_store(existing_checksum="oldchecksum")

    with patch("axiom.rag.embeddings.embed_texts", return_value=None):
        result = _upsert("sessions/changed.json", "Changed", "session", text, store, CORPUS_INTERNAL)

    assert result is True
    store.upsert_chunks.assert_called_once()


def test_upsert_passes_corpus_to_store():
    from axiom.rag.store import CORPUS_ORG
    text = "# Community content\n\nThis is a shared document for the org corpus.\n"
    store = _make_store()

    with patch("axiom.rag.embeddings.embed_texts", return_value=None):
        _upsert("test/doc.md", "Doc", "file", text, store, CORPUS_ORG)

    kwargs = store.upsert_chunks.call_args.kwargs
    assert kwargs.get("corpus") == CORPUS_ORG


def test_upsert_sets_source_metadata_on_chunks():
    """Chunk source_title and source_type should be overridden."""
    text = "# Session\n\nUser asked something. Assistant replied.\n"
    store = _make_store()
    captured_chunks = []

    def capture(chunks, embeddings, **kwargs):
        captured_chunks.extend(chunks)

    store.upsert_chunks.side_effect = capture

    with patch("axiom.rag.embeddings.embed_texts", return_value=None):
        _upsert("sessions/s.json", "My Session", "session", text, store, CORPUS_INTERNAL)

    assert all(c.source_title == "My Session" for c in captured_chunks)
    assert all(c.source_type == "session" for c in captured_chunks)


# ---------------------------------------------------------------------------
# ingest_session_file
# ---------------------------------------------------------------------------

def test_ingest_session_file_too_short(tmp_path):
    p = tmp_path / "short.json"
    p.write_text(json.dumps(_session_json([_user_msg("hi"), _assistant_msg("hello")])))
    store = _make_store()
    assert ingest_session_file(p, store) is False
    store.upsert_chunks.assert_not_called()


def test_ingest_session_file_success(tmp_path):
    messages = [_user_msg("what is xenon?"), _assistant_msg("Xe-135 is..."), _user_msg("thanks")]
    p = tmp_path / "session.json"
    p.write_text(json.dumps(_session_json(messages, title="Xenon Talk")))
    store = _make_store()

    with patch("axiom.rag.embeddings.embed_texts", return_value=None):
        result = ingest_session_file(p, store)

    assert result is True
    store.upsert_chunks.assert_called_once()


def test_ingest_session_file_uses_sessions_prefix(tmp_path):
    messages = [_user_msg("a"), _assistant_msg("b"), _user_msg("c")]
    p = tmp_path / "abc.json"
    p.write_text(json.dumps(_session_json(messages)))
    store = _make_store()

    with patch("axiom.rag.embeddings.embed_texts", return_value=None):
        ingest_session_file(p, store)

    # source_path passed to get_document should start with "sessions/"
    store.get_document.assert_called_once()
    source_path_arg = store.get_document.call_args.args[0]
    assert source_path_arg.startswith("sessions/")
    assert "abc.json" in source_path_arg


# ---------------------------------------------------------------------------
# ingest_sessions
# ---------------------------------------------------------------------------

def test_ingest_sessions_counts(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # One good session
    good = [_user_msg("x"), _assistant_msg("y"), _user_msg("z")]
    (sessions_dir / "good.json").write_text(json.dumps(_session_json(good)))

    # One too short
    bad = [_user_msg("a"), _assistant_msg("b")]
    (sessions_dir / "bad.json").write_text(json.dumps(_session_json(bad)))

    store = _make_store()
    with patch("axiom.rag.embeddings.embed_texts", return_value=None):
        indexed, skipped = ingest_sessions(sessions_dir, store)

    assert indexed == 1
    assert skipped == 1


def test_ingest_sessions_empty_dir(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    store = _make_store()
    indexed, skipped = ingest_sessions(sessions_dir, store)
    assert indexed == 0
    assert skipped == 0


# ---------------------------------------------------------------------------
# ingest_signals
# ---------------------------------------------------------------------------

def test_ingest_signals_counts(tmp_path):
    inbox = tmp_path / "processed"
    inbox.mkdir()

    (inbox / "good.json").write_text(json.dumps({"event": "system_alert", "severity": "high"}))
    (inbox / "empty.json").write_text("{}")

    store = _make_store()
    with patch("axiom.rag.embeddings.embed_texts", return_value=None):
        indexed, skipped = ingest_signals(inbox, store)

    assert indexed == 1
    assert skipped == 1


def test_ingest_signals_uses_signals_prefix(tmp_path):
    inbox = tmp_path / "processed"
    inbox.mkdir()
    (inbox / "sig.json").write_text(json.dumps({"key": "value"}))

    store = _make_store()
    with patch("axiom.rag.embeddings.embed_texts", return_value=None):
        ingest_signals(inbox, store)

    source_arg = store.get_document.call_args.args[0]
    assert source_arg.startswith("signals/")


# ---------------------------------------------------------------------------
# ingest_git_logs
# ---------------------------------------------------------------------------

def test_ingest_git_logs_no_repos(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    store = _make_store()
    indexed, skipped = ingest_git_logs(knowledge_dir, store)
    assert indexed == 0
    assert skipped == 0


def test_ingest_git_logs_finds_repo(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    repo = knowledge_dir / "my-repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()  # fake .git dir

    store = _make_store()
    fake_log = "### abc 2026-01-01\nInitial commit\n"

    with patch("axiom.rag.personal._git_log_text", return_value=fake_log):
        with patch("axiom.rag.embeddings.embed_texts", return_value=None):
            indexed, skipped = ingest_git_logs(knowledge_dir, store)

    assert indexed == 1
    assert skipped == 0


def test_ingest_git_logs_skips_on_empty_log(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    repo = knowledge_dir / "empty-repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()

    store = _make_store()
    with patch("axiom.rag.personal._git_log_text", return_value=None):
        indexed, skipped = ingest_git_logs(knowledge_dir, store)

    assert indexed == 0
    assert skipped == 1


def test_ingest_git_logs_uses_git_log_prefix(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    repo = knowledge_dir / "neutron-core"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()

    store = _make_store()
    with patch("axiom.rag.personal._git_log_text", return_value="### abc\ncommit\n"):
        with patch("axiom.rag.embeddings.embed_texts", return_value=None):
            ingest_git_logs(knowledge_dir, store)

    source_arg = store.get_document.call_args.args[0]
    assert source_arg.startswith("git-log/")
    assert "neutron-core" in source_arg
