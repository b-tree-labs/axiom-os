"""An empty store URL says nothing is configured, not that a scheme is wrong."""

import pytest

from axiom.rag.store_factory import create_store


@pytest.mark.parametrize("url", ["", "   "])
def test_no_store_url_names_the_setting_to_fix(url):
    with pytest.raises(ValueError, match="no RAG store is configured: set AXIOM_RAG_DSN"):
        create_store(url)


def test_the_platforms_own_database_url_spelling_is_accepted():
    """The node's database URL names its driver (postgresql+psycopg2://,
    axiom.infra.db.require_named_driver). Reusing it for the RAG store must
    work, not fail the /rag mount over a spelling. The store connects lazily,
    so constructing it here touches no database."""
    from axiom.rag.store import RAGStore
    from axiom.rag.store_factory import create_store

    store = create_store("postgresql+psycopg2://u:p@h:5432/db", ensure_schema=False)
    assert isinstance(store, RAGStore)
    assert store._dsn == "postgresql://u:p@h:5432/db"
