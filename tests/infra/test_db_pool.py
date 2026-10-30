

# --- a backend cannot be stuck forever --------------------------------------
#
# Restarting a service orphans whatever was mid-transaction: the client dies,
# the server keeps the transaction and its locks open because nothing tells it
# the other end is gone. One INSERT sat idle for 2h39m with 13 backends queued
# behind it, and the v1.6.80 deploy then failed twice at `ensure-schema` with
# `canceling statement due to lock timeout`.
#
# Clearing a stuck backend by hand is a remedy for one incident. These pin the
# remedy for the class.


def test_every_postgres_connection_carries_an_idle_in_transaction_timeout():
    from axiom.infra.db import pool_settings

    options = pool_settings("postgresql://h/db")["connect_args"]["options"]
    assert "idle_in_transaction_session_timeout" in options


def test_sqlite_is_left_alone():
    """SingletonThreadPool takes none of this, and there is no server to tell."""
    from axiom.infra.db import pool_settings

    assert pool_settings("sqlite:///x.db") == {}


def test_it_is_not_a_statement_timeout():
    """Killing a long QUERY is a different decision, and not this one.

    A conform pass or a migration legitimately runs for minutes. Only work
    that is idle *inside* a transaction is nobody's work.
    """
    from axiom.infra.db import _server_side_timeouts

    assert "statement_timeout" not in _server_side_timeouts()


def test_a_site_can_turn_it_off_and_owns_that(monkeypatch):
    """0 is Postgres's own spelling for never."""
    from axiom.infra.db import _server_side_timeouts

    monkeypatch.setenv("AXIOM_DB_IDLE_IN_TRANSACTION_MINUTES", "0")
    assert "=0min" in _server_side_timeouts()


def test_a_typo_does_not_take_the_database_down(monkeypatch):
    from axiom.infra.db import _server_side_timeouts

    monkeypatch.setenv("AXIOM_DB_IDLE_IN_TRANSACTION_MINUTES", "fifteen")
    assert "=15min" in _server_side_timeouts()
