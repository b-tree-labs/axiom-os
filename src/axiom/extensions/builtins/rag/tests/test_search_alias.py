# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`axi search` — the user-facing alias for the RAG index.

Two faults lived here at once, and both were silent in different ways: the
alias forwarded to a subcommand that does not exist, and it forwarded the
user's own verb along with it.
"""
from __future__ import annotations

from axiom.extensions.builtins.rag import cli as alias


class TestTheAliasForwardsCorrectly:
    def test_it_targets_a_subcommand_the_parser_accepts(self):
        """It forwarded "query" to a parser whose verb is "search", so the
        alias died on `invalid choice: 'query'` and printed the internal
        parser's entire verb list at a user who typed two words.

        The rag parser is built inside `main()`, so there is nothing to
        import and inspect; the registered verbs are read from the source
        instead. Asserting the two agree is the point either way.
        """
        import inspect
        import re

        import axiom.rag.cli as ragcli

        verbs = set(
            re.findall(r'sub\.add_parser\(\s*"([a-z0-9-]+)"', inspect.getsource(ragcli))
        )
        assert verbs, "no subcommands found — the scan itself is broken"
        assert alias._RAG_SEARCH_SUBCOMMAND in verbs, (
            f"the alias forwards to {alias._RAG_SEARCH_SUBCOMMAND!r}, which the "
            f"rag parser does not register: {sorted(verbs)}"
        )

    def test_it_does_not_repeat_the_verb_the_user_typed(self, monkeypatch):
        """`sys.argv[1:]` still holds "search", so prepending the subcommand
        made the query "search <query>" — every result quietly worse, with
        nothing on screen to say why."""
        seen = {}
        monkeypatch.setattr(alias, "sys", __import__("sys"))
        monkeypatch.setattr("sys.argv", ["axi", "search", "reactor site criteria"])
        monkeypatch.setattr(
            "axiom.rag.cli.main", lambda argv=None: seen.update(argv=argv)
        )
        alias.main_search()
        assert seen["argv"] == ["search", "reactor site criteria"], seen["argv"]

    def test_an_explicit_argv_is_passed_through_untouched(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "axiom.rag.cli.main", lambda argv=None: seen.update(argv=argv)
        )
        alias.main_search(["some query"])
        assert seen["argv"] == ["search", "some query"]

    def test_a_query_that_happens_to_start_with_the_word_search(self, monkeypatch):
        """Only the leading VERB is stripped, and only from sys.argv. A query
        that begins with the word survives when passed explicitly."""
        seen = {}
        monkeypatch.setattr(
            "axiom.rag.cli.main", lambda argv=None: seen.update(argv=argv)
        )
        alias.main_search(["search engines for reactors"])
        assert seen["argv"] == ["search", "search engines for reactors"]


class TestTheDatabaseIsResolvedByConnecting:
    """RAG built a URL from a stored secret that nothing else on the platform
    honours, so when the two drifted RAG alone could not reach the database
    while `axi status` reported it healthy.
    """

    def test_it_returns_the_first_candidate_that_connects(self, monkeypatch):
        from axiom.rag import cli as ragcli

        monkeypatch.setattr(
            ragcli,
            "_candidate_urls",
            lambda: [("first", "postgresql://no"), ("second", "postgresql://yes")],
        )
        attempts = []

        class _Conn:
            def close(self):
                pass

        def fake_connect(url, **kw):
            attempts.append(url)
            if url == "postgresql://no":
                raise RuntimeError("nope")
            return _Conn()

        monkeypatch.setitem(
            __import__("sys").modules, "psycopg2", type("m", (), {"connect": fake_connect})
        )
        assert ragcli._first_reachable_url() == "postgresql://yes"
        assert attempts == ["postgresql://no", "postgresql://yes"]

    def test_it_returns_empty_when_nothing_connects(self, monkeypatch):
        from axiom.rag import cli as ragcli

        monkeypatch.setattr(ragcli, "_candidate_urls", lambda: [("only", "postgresql://x")])

        def fake_connect(url, **kw):
            raise RuntimeError("nope")

        monkeypatch.setitem(
            __import__("sys").modules, "psycopg2", type("m", (), {"connect": fake_connect})
        )
        assert ragcli._first_reachable_url() == ""

    def test_a_password_never_reaches_the_log(self, monkeypatch, caplog):
        """Candidates are labelled, not printed: a connection string carries
        a credential."""
        import logging

        from axiom.rag import cli as ragcli

        monkeypatch.setattr(
            ragcli,
            "_candidate_urls",
            lambda: [("stored secret", "postgresql://axiom:hunter2@localhost/db")],
        )

        def fake_connect(url, **kw):
            raise RuntimeError("denied")

        monkeypatch.setitem(
            __import__("sys").modules, "psycopg2", type("m", (), {"connect": fake_connect})
        )
        with caplog.at_level(logging.DEBUG, logger="axiom.rag.cli"):
            ragcli._first_reachable_url()
        assert "hunter2" not in caplog.text
        assert "stored secret" in caplog.text

    def test_a_password_never_reaches_the_log_on_success_either(
        self, monkeypatch, caplog
    ):
        """The failing path and the succeeding path log separately, and only
        the failing one was covered — so a mutant that logged the whole URL
        on success passed the suite."""
        import logging

        from axiom.rag import cli as ragcli

        monkeypatch.setattr(
            ragcli,
            "_candidate_urls",
            lambda: [("stored secret", "postgresql://axiom:hunter2@localhost/db")],
        )

        class _Conn:
            def close(self):
                pass

        monkeypatch.setitem(
            __import__("sys").modules,
            "psycopg2",
            type("m", (), {"connect": lambda url, **kw: _Conn()}),
        )
        with caplog.at_level(logging.DEBUG, logger="axiom.rag.cli"):
            chosen = ragcli._first_reachable_url()
        assert chosen == "postgresql://axiom:hunter2@localhost/db"
        assert "hunter2" not in caplog.text, "the chosen URL was logged with its password"
        assert "stored secret" in caplog.text
