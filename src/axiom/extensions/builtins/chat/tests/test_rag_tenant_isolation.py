# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One tenant's material must not come back in another tenant's prompt.

``test_rag_session_tenancy`` covers a different axis: the operator's own
corpus versus what a served surface may read. That axis is enforced. This
one is not, and these tests are what make it so.

The shape of the gap it closes: ``search()`` could be scoped only by corpus,
and every served surface declared the same two corpora, so every tenant
retrieved from one undifferentiated pool. Conversations were already scoped
by ``tenant_id``; retrieval was not scoped by anything.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.scope import (
    OPERATOR_LOCAL_CORPUS,
    SHARED_CORPORA,
    shared_corpora_for,
)
from axiom.rag.store import CORPUS_COMMUNITY, CORPUS_ORG, tenant_corpus


class TestATenantReadsItsOwnAndTheCommons:
    def test_a_tenant_gets_the_community_corpus_and_its_own(self):
        assert shared_corpora_for("senna") == (CORPUS_COMMUNITY, tenant_corpus("senna"))

    def test_a_tenant_does_not_get_the_org_corpus(self):
        """rag-org holds the operator organization's material, not a guest's."""
        assert CORPUS_ORG not in shared_corpora_for("senna")

    def test_a_tenant_does_not_get_the_operators_corpus(self):
        assert OPERATOR_LOCAL_CORPUS not in shared_corpora_for("senna")

    def test_two_tenants_share_only_the_commons(self):
        a = set(shared_corpora_for("senna"))
        b = set(shared_corpora_for("prost"))
        assert a & b == {CORPUS_COMMUNITY}

    def test_no_tenant_can_name_anothers_corpus(self):
        assert tenant_corpus("senna") not in shared_corpora_for("prost")


class TestTheUntenantedCaseIsTodaysBehaviour:
    """A single-tenant install keeps working, but by declaration."""

    def test_none_means_the_single_tenant_install(self):
        assert shared_corpora_for(None) == tuple(SHARED_CORPORA)

    def test_that_is_still_never_the_operators_corpus(self):
        assert OPERATOR_LOCAL_CORPUS not in shared_corpora_for(None)


class TestAMalformedTenantNarrowsOrRefuses:
    """The failure mode to avoid is a bad tenant id WIDENING the read."""

    @pytest.mark.parametrize("hostile", ["", "   ", "a:b", "SENNA", "../x", "a" * 200])
    def test_a_malformed_tenant_is_refused_not_ignored(self, hostile):
        with pytest.raises(ValueError):
            shared_corpora_for(hostile)

    def test_the_empty_string_does_not_silently_become_the_untenanted_case(self):
        """'' is a present-but-broken tenant, not an absent one."""
        with pytest.raises(ValueError):
            shared_corpora_for("")
        assert shared_corpora_for(None) == tuple(SHARED_CORPORA)

    def test_a_refusal_never_returns_a_wider_set(self):
        for hostile in ("", "a:b", "SENNA"):
            try:
                got = shared_corpora_for(hostile)
            except ValueError:
                continue
            pytest.fail(f"{hostile!r} returned {got!r} instead of refusing")


class TestTheScopeCarriesTheTenantToTheRetriever:
    """A declaration nothing reads is not an isolation boundary."""

    @pytest.fixture
    def headless(self):
        from unittest.mock import MagicMock

        from axiom.extensions.builtins.chat.headless import HeadlessChat

        return HeadlessChat(gateway=MagicMock(), turn_deadline=30.0)

    def test_new_scope_declares_the_tenants_corpora(self, headless):
        scope = headless.new_scope(tenant_id="senna")
        assert scope.retrieval_corpora == [CORPUS_COMMUNITY, tenant_corpus("senna")]

    def test_new_scope_without_a_tenant_is_unchanged(self, headless):
        assert headless.new_scope().retrieval_corpora == list(SHARED_CORPORA)

    def test_two_tenants_get_two_scopes(self, headless):
        a = headless.new_scope(tenant_id="senna").retrieval_corpora
        b = headless.new_scope(tenant_id="prost").retrieval_corpora
        assert a != b
        assert tenant_corpus("senna") not in b
        assert tenant_corpus("prost") not in a

    def test_a_tenant_scope_still_refuses_the_operators_corpus(self, headless):
        assert OPERATOR_LOCAL_CORPUS not in headless.new_scope(tenant_id="senna").retrieval_corpora

    def test_a_tenant_scope_still_does_not_index_its_transcript(self, headless):
        assert headless.new_scope(tenant_id="senna").index_transcript is False

    def test_a_malformed_tenant_fails_scope_construction(self, headless):
        with pytest.raises(ValueError):
            headless.new_scope(tenant_id="a:b")
