# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One person's conversation must not come back in another person's prompt.

There is a round trip through the retrieval corpus. After every turn,
``_schedule_session_index`` indexes that conversation's session file into the
node's internal corpus. Before every turn, ``_rag_context`` retrieves into the
system prompt. The write goes to ``rag-internal``; the read, with no corpus
filter, draws from all three corpora, ``rag-internal`` among them. So whatever
one conversation put in is what another conversation can pull out.

At a terminal that round trip is the feature: an operator asking about
something they discussed last week gets last week's conversation back, because
the operator on both ends is the same person. On a surface answering other
people they are different people, and the same round trip is a disclosure. It
is the retrieval half of the leak the operator-local prompt sources had.

What is live today, and what is latent
--------------------------------------

The two halves have different reach, and these tests keep them apart rather
than asserting one shape for both.

The **read** half is live on every served turn. A serving worker on an
operator's machine retrieves from the operator's own corpus, which is where
that machine's terminal transcripts are indexed after every terminal turn. No
serving surface has to persist anything for this: the operator's terminal put
the material there.

The **write** half needs a session file on disk, and the shipped serving path
(``serve.agent_backend``) builds a fresh in-memory ``Session`` per request and
never saves it, so today it indexes nothing. That is an accident of which
surfaces call ``SessionStore.save`` rather than a decision anybody wrote down:
``HeadlessChat.new_scope(session=...)`` exists precisely so a surface can carry
a conversation across requests, and the moment one persists it the write half
fires. So the two-requester test below plays that surface, saving each
conversation exactly as the terminal surfaces do, and the fix closes the seam
before a surface walks into it.

Both mitigations that already exist are kept: a joined node's served store has
no ``upsert_chunks`` so it is never indexed into, and the federation endpoint
that serves a peer never serves ``rag-internal``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat import agent as agent_mod
from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.headless import HeadlessChat
from axiom.extensions.builtins.chat.scope import (
    OPERATOR_LOCAL_CORPUS,
    SHARED_CORPORA,
    ChatScope,
)
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway
from axiom.infra.orchestrator.session import Session, SessionStore

# A token distinctive enough that finding it in a prompt is proof of the round
# trip and not of a coincidental match on ordinary English.
ALICE_SECRET = "ZORPTANGLE7741"
OPERATOR_SECRET = "QUILLMARROW3308"

# What the second person asks. It is the probing question, not a contrived
# query: the leak is only interesting if asking about the material finds it.
PROBE = f"clearance passphrase {ALICE_SECRET}"
OPERATOR_PROBE = f"clearance passphrase {OPERATOR_SECRET}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_gateway():
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    gw.complete_with_tools.return_value = CompletionResponse(
        text="ok", provider="test", success=True
    )
    return gw


@pytest.fixture
def no_embeddings(monkeypatch):
    """No embedding provider configured, which is the offline default.

    ``embed_texts`` returns ``None`` rather than raising when nothing is
    configured, so both halves still work on keyword ranking alone: the
    transcript is indexed with null embeddings and the retrieval finds it
    through full-text search. Pinned here so the tests never reach the network
    and never depend on a provider being up.
    """
    import axiom.rag.embeddings as embeddings
    import axiom.rag.personal as personal

    monkeypatch.setattr(embeddings, "embed_texts", lambda *a, **k: None)
    monkeypatch.setattr(personal, "embed_texts", lambda *a, **k: None)


@pytest.fixture
def machine(monkeypatch, tmp_path, no_embeddings):
    """One machine: a repository root, a corpus, and a session directory.

    Everything a turn touches is under ``tmp_path``. The corpus is a real
    SQLite store rather than a mock, so the assertions below are about what
    actually round-trips rather than about what a stub was told.
    """
    from axiom.extensions.builtins.settings import store as settings_mod

    root = tmp_path / "machine"
    sessions = root / "runtime" / "sessions"
    sessions.mkdir(parents=True)
    corpus_url = f"sqlite:///{root}/rag.db"

    class _Settings:
        def get(self, key, default=None):
            return corpus_url if key == "rag.database_url" else default

    monkeypatch.setattr(settings_mod, "SettingsStore", _Settings)
    monkeypatch.setattr(agent_mod, "_REPO_ROOT", root)

    # The indexer's daemon thread, run inline, so a test observes its effect
    # instead of racing it.
    class _SyncThread:
        def __init__(self, target, daemon=False):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(agent_mod, "threading", SimpleNamespace(Thread=_SyncThread))

    return SimpleNamespace(
        root=root,
        corpus_url=corpus_url,
        sessions=SessionStore(sessions_dir=sessions),
        sessions_dir=sessions,
    )


@pytest.fixture
def headless(machine, mock_gateway, tmp_path):
    """A serving worker on that machine."""
    return HeadlessChat(
        turn_deadline=30.0,
        gateway=mock_gateway,
        bus=EventBus(log_path=tmp_path / "served-events.jsonl"),
    )


@pytest.fixture
def terminal(machine, mock_gateway, tmp_path):
    """The operator at a keyboard on that same machine: no scope, no policy."""
    return ChatAgent(
        gateway=mock_gateway,
        bus=EventBus(log_path=tmp_path / "terminal-events.jsonl"),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _systems_sent(gateway) -> list[str]:
    """Every system prompt the gateway was actually called with, in order."""
    return [call.kwargs["system"] for call in gateway.complete_with_tools.call_args_list]


def _seed_conversation(machine, session_id: str, principal: str, secret: str) -> Session:
    """A conversation already under way, persisted the way a surface persists.

    Three messages, because ``personal._MIN_SESSION_MESSAGES`` is the floor
    below which a transcript is judged too small to index; a two-message seed
    would make every assertion below pass for the wrong reason.
    """
    session = Session(session_id=session_id)
    session.principal_id = principal
    session.add_message("user", f"My clearance passphrase is {secret}.")
    session.add_message("assistant", f"Noted. {secret} is recorded.")
    session.add_message("user", f"Keep {secret} to yourself.")
    machine.sessions.save(session)
    return session


def _corpus_holds(machine, needle: str) -> bool:
    """Whether *needle* is retrievable from the corpus by any caller at all.

    The administrative read: no corpus filter, no access context. Used to tell
    "nothing leaked" apart from "nothing was ever indexed", which is the way a
    tenancy test passes while proving nothing.
    """
    from axiom.rag.retriever import retrieve
    from axiom.rag.sqlite_store import SQLiteRAGStore

    store = SQLiteRAGStore(machine.corpus_url)
    store.connect()
    try:
        hits = retrieve(store=store, query_text=needle, query_embedding=None, limit=8)
    finally:
        store.close()
    return any(needle in hit.chunk_text for hit in hits)


# ---------------------------------------------------------------------------
# The leak, end to end
# ---------------------------------------------------------------------------


class TestOneRequestersConversationDoesNotReachAnother:
    """Two people, one serving worker, one corpus between them."""

    def test_the_second_requesters_prompt_does_not_carry_the_firsts_conversation(
        self, machine, headless, mock_gateway
    ):
        """The central case: A's turn, then B's, and B's prompt is clean.

        The surface here persists each conversation, which is what a surface
        continuing a conversation across requests does, and is the precondition
        the write half needs.
        """
        alice = _seed_conversation(machine, "alice", "@axi:alice", ALICE_SECRET)
        scope_a = headless.new_scope(session=alice)
        headless.turn("Remind me what we agreed.", stream=False, scope=scope_a)
        machine.sessions.save(alice)

        bob = Session(session_id="bob")
        bob.principal_id = "@axi:bob"
        scope_b = headless.new_scope(session=bob)
        headless.turn(PROBE, stream=False, scope=scope_b)
        machine.sessions.save(bob)

        assert scope_a.session.principal_id != scope_b.session.principal_id
        bobs_prompt = _systems_sent(mock_gateway)[1]
        assert ALICE_SECRET not in bobs_prompt
        assert "clearance passphrase" not in bobs_prompt

    def test_the_first_conversation_is_not_indexed_by_a_served_turn(self, machine, headless):
        """The write half: a served surface contributes nothing to the corpus."""
        alice = _seed_conversation(machine, "alice", "@axi:alice", ALICE_SECRET)
        scope = headless.new_scope(session=alice)
        headless.turn("Remind me what we agreed.", stream=False, scope=scope)
        assert not _corpus_holds(machine, ALICE_SECRET)

    def test_the_session_file_the_write_half_would_have_read_really_exists(self, machine, headless):
        """Guards the two tests above: no file means they prove nothing.

        The transcript is on disk, holds the secret, and carries enough
        messages to clear the indexer's floor. Everything the write half needs
        is present, and the only thing stopping it is the declaration.
        """
        _seed_conversation(machine, "alice", "@axi:alice", ALICE_SECRET)
        path = machine.sessions_dir / "alice.json"
        assert path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert ALICE_SECRET in path.read_text(encoding="utf-8")
        assert len(data["messages"]) >= 3


class TestTheOperatorsOwnTranscriptDoesNotReachARequester:
    """The half that is live with no hypothetical surface at all.

    The operator's terminal indexes its transcripts after every turn. A serving
    worker on the same machine reads the same corpus. Nothing on the serving
    side has to persist anything for the operator's conversation to arrive in
    a stranger's prompt.
    """

    def test_the_operators_terminal_transcript_reaches_the_corpus(self, machine, terminal):
        """The round trip works, which is what makes the next test meaningful."""
        operator = _seed_conversation(machine, "operator", "@axi:operator", OPERATOR_SECRET)
        terminal.session = operator
        terminal.turn("Remind me what we agreed.", stream=False)
        assert _corpus_holds(machine, OPERATOR_SECRET)

    def test_a_served_prompt_does_not_carry_the_operators_transcript(
        self, machine, terminal, headless, mock_gateway
    ):
        operator = _seed_conversation(machine, "operator", "@axi:operator", OPERATOR_SECRET)
        terminal.session = operator
        terminal.turn("Remind me what we agreed.", stream=False)
        assert _corpus_holds(machine, OPERATOR_SECRET), "fixture: nothing was indexed"

        headless.turn(OPERATOR_PROBE, stream=False)
        served_prompt = _systems_sent(mock_gateway)[-1]
        assert OPERATOR_SECRET not in served_prompt
        assert "clearance passphrase" not in served_prompt


# ---------------------------------------------------------------------------
# The terminal is untouched
# ---------------------------------------------------------------------------


class TestTheTerminalIsUnchanged:
    """An operator at a keyboard keeps the round trip that was built for them."""

    def test_the_operator_still_retrieves_their_own_history(self, machine, terminal, mock_gateway):
        operator = _seed_conversation(machine, "operator", "@axi:operator", OPERATOR_SECRET)
        terminal.session = operator
        terminal.turn("Remind me what we agreed.", stream=False)
        machine.sessions.save(operator)

        later = Session(session_id="operator-later")
        terminal.session = later
        terminal.turn(OPERATOR_PROBE, stream=False)

        assert OPERATOR_SECRET in _systems_sent(mock_gateway)[-1]

    def test_the_operator_still_indexes_their_transcript(self, machine, terminal):
        operator = _seed_conversation(machine, "operator", "@axi:operator", OPERATOR_SECRET)
        terminal.session = operator
        terminal.turn("Remind me what we agreed.", stream=False)
        assert _corpus_holds(machine, OPERATOR_SECRET)

    def test_a_terminal_retrieval_asks_for_no_corpus_filter(self, machine, terminal, monkeypatch):
        """``None`` is every corpus, which is exactly what it has always sent."""
        seen = _record_retriever_calls(monkeypatch)
        terminal.session = _seed_conversation(machine, "op", "@axi:operator", OPERATOR_SECRET)
        terminal.turn("Remind me what we agreed.", stream=False)
        assert seen and all(call["corpora"] is None for call in seen)

    def test_declaring_the_defaults_composes_the_identical_prompt(self, machine, terminal):
        """Writing the default down changes nothing: same characters, both ways.

        Compared after the transcript is in the corpus, so both prompts carry a
        retrieved block. Comparing two empty blocks would make any default
        agree with any other.
        """
        operator = _seed_conversation(machine, "op", "@axi:operator", OPERATOR_SECRET)
        terminal.session = operator
        terminal.turn("Remind me what we agreed.", stream=False)
        assert _corpus_holds(machine, OPERATOR_SECRET), "fixture: nothing was indexed"

        later = Session(session_id="op-later")
        later.add_message("user", OPERATOR_PROBE)
        implicit = terminal._build_system_prompt(scope=ChatScope(session=later))
        explicit = terminal._build_system_prompt(
            scope=ChatScope(
                session=later,
                retrieval_corpora=None,
                index_transcript=True,
            )
        )
        assert OPERATOR_SECRET in implicit, "fixture: nothing was retrieved to compare"
        assert implicit == explicit


# ---------------------------------------------------------------------------
# What reaches the retriever
# ---------------------------------------------------------------------------


def _record_retriever_calls(monkeypatch) -> list[dict]:
    """Capture every keyword argument ``_rag_context`` hands the retriever."""
    import axiom.rag.retriever as retriever_mod

    seen: list[dict] = []
    real = retriever_mod.retrieve

    def _spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(retriever_mod, "retrieve", _spy)
    return seen


class TestTheCorporaReachTheRetriever:
    """A declaration that never arrives at the query is not a filter."""

    def test_a_served_turn_names_the_shared_corpora(self, machine, headless, monkeypatch):
        seen = _record_retriever_calls(monkeypatch)
        headless.turn(PROBE, stream=False)
        assert seen, "a served turn retrieved nothing at all"
        for call in seen:
            assert call["corpora"] == list(SHARED_CORPORA)
            assert OPERATOR_LOCAL_CORPUS not in call["corpora"]

    def test_the_corpora_reach_the_store_query(self, machine, headless, monkeypatch):
        """One more layer down: the store is the thing that filters."""
        from axiom.rag.sqlite_store import SQLiteRAGStore

        seen: list[list[str] | None] = []
        real = SQLiteRAGStore.search

        def _spy(self, *args, **kwargs):
            seen.append(kwargs.get("corpora"))
            return real(self, *args, **kwargs)

        monkeypatch.setattr(SQLiteRAGStore, "search", _spy)
        headless.turn(PROBE, stream=False)
        assert seen, "a served turn never queried the store"
        for corpora in seen:
            assert corpora == list(SHARED_CORPORA)

    def test_the_scopes_declaration_is_what_arrives(self, machine, terminal, monkeypatch):
        """Not a constant in the call site: change the scope, change the query."""
        seen = _record_retriever_calls(monkeypatch)
        terminal.turn(
            PROBE,
            stream=False,
            scope=ChatScope(retrieval_corpora=["rag-community"]),
        )
        assert seen and all(call["corpora"] == ["rag-community"] for call in seen)

    def test_the_call_site_hands_over_no_access_context(self, machine, headless, monkeypatch):
        """Deliberate, and the reason the fix is a corpus filter.

        An ``AccessContext`` on its own filters nothing here: every lookup the
        retriever would consult is optional, and with none of them wired the
        tier reads ``public``, the classification ``unclassified`` and the site
        ``None``, which permits every chunk. Passing one would look like a
        control and be none, so the call site passes none and says so.
        """
        seen = _record_retriever_calls(monkeypatch)
        headless.turn(PROBE, stream=False)
        assert seen
        for call in seen:
            assert "access_context" not in call
            assert "tier_lookup" not in call

    def test_an_access_context_alone_would_have_permitted_everything(self):
        """The claim the test above rests on, pinned against the retriever."""
        from axiom.rag.retriever import AccessContext, _permits

        assert _permits(AccessContext(), "public", "unclassified", None, None)

    def test_the_retriever_filters_only_when_an_access_context_is_given(self):
        """The other half of that claim, through ``retrieve`` rather than under it.

        Wired with a lookup that a default context would refuse, so what is
        asserted is that the filter does not run, rather than that it ran and
        happened to agree. This is the reason a corpus filter is the fix and an
        access context would not have been.
        """
        from axiom.rag.retriever import AccessContext, retrieve

        class _Hit:
            source_path = "classified.md"
            source_title = "t"
            chunk_text = "body"
            chunk_index = 0
            corpus = "rag-org"
            similarity = 0.9

        class _Store:
            def search(self, query_embedding=None, query_text="", corpora=None, limit=5, **kw):
                return [_Hit()]

        classified = {"tier_lookup": lambda _path: "classified"}
        assert len(retrieve(_Store(), "q", None, limit=5, **classified)) == 1
        denied = retrieve(
            _Store(), "q", None, limit=5, access_context=AccessContext(), **classified
        )
        assert denied == []


# ---------------------------------------------------------------------------
# The declarations
# ---------------------------------------------------------------------------


class TestTheDeclarationsDefaultSafe:
    """The surface says what it may do; the platform never guesses."""

    def test_the_scope_defaults_to_every_corpus(self):
        assert ChatScope().retrieval_corpora is None

    def test_the_scope_defaults_to_indexing_its_transcript(self):
        assert ChatScope().index_transcript is True

    def test_a_headless_scope_declares_the_shared_corpora(self, machine, headless):
        assert headless.new_scope().retrieval_corpora == list(SHARED_CORPORA)

    def test_a_headless_scope_declares_no_transcript_indexing(self, machine, headless):
        assert headless.new_scope().index_transcript is False

    def test_the_shared_corpora_are_every_corpus_but_the_operators(self):
        """Pins the constant against the corpus names it stands in for.

        ``scope`` cannot import the RAG store at module level without dragging
        a database driver into every chat import, so it names the corpora
        itself. This is the seam that would drift, so it is asserted rather
        than trusted, and a fourth corpus fails here until somebody decides
        which side it belongs on.
        """
        from axiom.rag.store import ALL_CORPORA, CORPUS_INTERNAL

        assert OPERATOR_LOCAL_CORPUS == CORPUS_INTERNAL
        assert set(SHARED_CORPORA) == set(ALL_CORPORA) - {CORPUS_INTERNAL}

    def test_the_default_scope_of_a_terminal_agent_declares_the_terminal_answer(self, terminal):
        assert terminal.scope.retrieval_corpora is None
        assert terminal.scope.index_transcript is True

    def test_one_agent_answers_two_requests_differently(self, machine, terminal, mock_gateway):
        """Why the declarations are on the scope: per request, not per process."""
        operator = _seed_conversation(machine, "operator", "@axi:operator", OPERATOR_SECRET)
        terminal.session = operator
        terminal.turn("Remind me what we agreed.", stream=False)

        terminal.turn(
            OPERATOR_PROBE,
            stream=False,
            scope=ChatScope(retrieval_corpora=list(SHARED_CORPORA), index_transcript=False),
        )
        terminal.session = Session(session_id="operator-later")
        terminal.turn(OPERATOR_PROBE, stream=False)

        served, operators_own = _systems_sent(mock_gateway)[-2:]
        assert OPERATOR_SECRET not in served
        assert OPERATOR_SECRET in operators_own


class TestHandBuiltScopes:
    """The same guard shape the turn deadline already has."""

    def _servable(self, **overrides) -> ChatScope:
        """A scope that clears every guard but the one under test."""
        fields = {
            "turn_deadline": 30.0,
            "include_operator_local_prompts": False,
            "retrieval_corpora": list(SHARED_CORPORA),
            "index_transcript": False,
        }
        fields.update(overrides)
        return ChatScope(**fields)

    def test_a_headless_turn_refuses_a_scope_that_would_index(self, machine, headless):
        with pytest.raises(ValueError, match="index"):
            headless.turn("q", stream=False, scope=self._servable(index_transcript=True))

    def test_a_headless_turn_refuses_a_scope_that_could_read_the_operators_corpus(
        self, machine, headless
    ):
        scope = self._servable(retrieval_corpora=["rag-community", OPERATOR_LOCAL_CORPUS])
        with pytest.raises(ValueError, match=OPERATOR_LOCAL_CORPUS):
            headless.turn("q", stream=False, scope=scope)

    def test_a_headless_turn_refuses_an_unfiltered_scope(self, machine, headless):
        """``None`` means every corpus, so it is refused like naming it would be."""
        with pytest.raises(ValueError, match=OPERATOR_LOCAL_CORPUS):
            headless.turn("q", stream=False, scope=self._servable(retrieval_corpora=None))

    def test_both_refusals_name_the_way_to_build_the_scope(self, machine, headless):
        for scope in (
            self._servable(index_transcript=True),
            self._servable(retrieval_corpora=None),
        ):
            with pytest.raises(ValueError, match="new_scope"):
                headless.turn("q", stream=False, scope=scope)

    def test_a_scope_from_new_scope_is_accepted(self, machine, headless, mock_gateway):
        headless.turn("q", stream=False, scope=headless.new_scope())
        assert mock_gateway.complete_with_tools.call_count == 1

    def test_the_declarations_survive_a_turn(self, machine, headless):
        """Nothing writes them back, so turn N cannot widen turn N+1."""
        scope = headless.new_scope()
        headless.turn("first", stream=False, scope=scope)
        headless.turn("second", stream=False, scope=scope)
        assert scope.retrieval_corpora == list(SHARED_CORPORA)
        assert scope.index_transcript is False


# ---------------------------------------------------------------------------
# The mitigations that already existed
# ---------------------------------------------------------------------------


class TestTheExistingMitigationsStand:
    """Neither of the two guards already in place is disturbed."""

    def test_a_joined_nodes_served_store_is_still_not_indexed_into(self, monkeypatch, tmp_path):
        import axiom.rag.personal as personal
        from axiom.extensions.builtins.settings import store as settings_mod

        root = tmp_path / "joined"
        (root / "runtime" / "sessions").mkdir(parents=True)
        (root / "runtime" / "sessions" / "s1.json").write_text("{}", encoding="utf-8")

        class _Settings:
            def get(self, key, default=None):
                return "http://site.example/" if key == "rag.database_url" else default

        class _SyncThread:
            def __init__(self, target, daemon=False):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr(settings_mod, "SettingsStore", _Settings)
        monkeypatch.setattr(agent_mod, "_REPO_ROOT", root)
        monkeypatch.setattr(agent_mod, "threading", SimpleNamespace(Thread=_SyncThread))

        calls: list = []
        monkeypatch.setattr(personal, "ingest_session_file", lambda *a, **k: calls.append(a))

        agent = ChatAgent.__new__(ChatAgent)
        agent.session = SimpleNamespace(session_id="s1")
        agent._schedule_session_index()

        assert calls == [], "a served (read-only) store has no local corpus to index into"

    def test_the_peer_endpoint_still_never_serves_the_operators_corpus(self):
        from axiom.extensions.builtins.http.federation_endpoint import _tier_to_corpora

        for tier in ("community", "restricted", "anything-else"):
            assert OPERATOR_LOCAL_CORPUS not in _tier_to_corpora(tier)


# ---------------------------------------------------------------------------
# What the docstrings claim
# ---------------------------------------------------------------------------


class TestTheDocumentedBehaviourIsTheRealBehaviour:
    """House rule: a testable claim in a docstring gets a test."""

    def test_the_indexer_reads_the_file_as_the_surface_last_saved_it(self, machine, terminal):
        """The turn in flight is not in the file yet, and the docstring says so.

        The surfaces that persist a session call ``SessionStore.save`` after
        ``turn`` returns, and the indexer runs inside ``turn``. So what gets
        indexed is the conversation as of the previous save. The method used to
        claim the opposite.
        """
        operator = _seed_conversation(machine, "operator", "@axi:operator", OPERATOR_SECRET)
        terminal.session = operator
        terminal.turn(f"and also {ALICE_SECRET}", stream=False)

        assert _corpus_holds(machine, OPERATOR_SECRET)
        assert not _corpus_holds(machine, ALICE_SECRET)

    def test_nothing_is_indexed_before_a_surface_has_saved_anything(self, machine, headless):
        """No file, no index: the shipped serving path's whole protection."""
        headless.turn("first contact", stream=False)
        assert list(machine.sessions_dir.glob("*.json")) == []
        assert not _corpus_holds(machine, "first contact")
