"""An empty store URL says nothing is configured, not that a scheme is wrong."""

import pytest

from axiom.rag.store_factory import create_store


@pytest.mark.parametrize("url", ["", "   "])
def test_no_store_url_names_the_setting_to_fix(url):
    with pytest.raises(ValueError, match="no RAG store is configured: set AXIOM_RAG_DSN"):
        create_store(url)
