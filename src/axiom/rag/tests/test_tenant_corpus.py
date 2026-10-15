"""Tenant-scoped corpora: the retrieval isolation axis.

Before this, retrieval could be scoped only by corpus, and every served
surface read the same two corpora. A partner therefore retrieved from the
same undifferentiated pool as every other partner. These tests pin the
naming, the validation that makes the name an isolation boundary rather
than a string concatenation, and the refusal that keeps a missing tenant
from widening the read instead of narrowing it.
"""

from __future__ import annotations

import pytest

from axiom.rag.store import (
    CORPUS_COMMUNITY,
    CORPUS_INTERNAL,
    CORPUS_ORG,
    CORPUS_TENANT_PREFIX,
    is_tenant_corpus,
    tenant_corpus,
    tenant_of,
)


class TestTenantCorpusNaming:
    def test_tenant_corpus_is_prefixed(self):
        assert tenant_corpus("senna") == f"{CORPUS_TENANT_PREFIX}senna"

    def test_round_trips(self):
        assert tenant_of(tenant_corpus("prost")) == "prost"

    def test_distinct_tenants_get_distinct_corpora(self):
        assert tenant_corpus("senna") != tenant_corpus("prost")

    def test_is_tenant_corpus_discriminates(self):
        assert is_tenant_corpus(tenant_corpus("senna"))
        for fixed in (CORPUS_COMMUNITY, CORPUS_ORG, CORPUS_INTERNAL):
            assert not is_tenant_corpus(fixed)

    def test_tenant_of_returns_none_for_a_fixed_corpus(self):
        assert tenant_of(CORPUS_ORG) is None


class TestTenantIdIsAnIsolationBoundary:
    """A tenant id reaches this from an API field, so it is untrusted input.

    If a caller can shape the resulting corpus name, it can name another
    tenant's corpus, and the isolation is decorative.
    """

    @pytest.mark.parametrize(
        "hostile",
        [
            "",  # empty: must not produce a bare-prefix corpus
            "   ",  # whitespace only
            "a:b",  # embeds the separator -> could forge a name
            "senna ",  # trailing space, distinct string same intent
            " senna",
            "SENNA",  # case would make two corpora for one tenant
            "../senna",
            "senna\nvcu",  # newline
            "senna\x00",  # NUL
            "a" * 200,  # unbounded length
            "-senna",  # must start alnum
        ],
    )
    def test_refuses_a_tenant_id_that_could_forge_or_split_a_corpus(self, hostile):
        with pytest.raises(ValueError):
            tenant_corpus(hostile)

    def test_refuses_a_non_string(self):
        with pytest.raises((ValueError, TypeError)):
            tenant_corpus(None)  # type: ignore[arg-type]

    def test_accepts_ordinary_ids(self):
        for ok in ("senna", "prost", "fangio", "ut-austin", "site.one", "a", "x_1"):
            assert tenant_corpus(ok).startswith(CORPUS_TENANT_PREFIX)
