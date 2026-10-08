# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A site that changed name is one site, not two.

A site id was whatever string a credential or a manifest happened to
carry, and nothing resolved it to an identity — so two spellings of one
site were two sites, permanently and silently.

It happened twice. One reactor held 246 GB of history under one id while
the surface a person asks served 7,307 rows under another, and asking the
second for the nine-year record returned 404. One flow loop had its data
under one id and its site package named after another, found only because
somebody moved a channel map between repos.

Neither was a typo. Both are what happens when a name changes and nothing
in the platform can say the two names are the same thing.
"""

from __future__ import annotations

import pytest

from axiom.infra.site_identity import SiteIdentities, UnknownSite


@pytest.fixture
def sites():
    registry = SiteIdentities()
    registry.declare("site-a", also_known_as=("SITE-A-OLD", "site-a-old"))
    registry.declare("site-d", also_known_as=("site-d-old",))
    registry.declare("site-b")
    return registry


class TestAFormerNameResolves:
    def test_the_old_id_means_the_new_site(self, sites):
        assert sites.resolve("SITE-A-OLD") == "site-a"

    def test_the_canonical_id_is_itself(self, sites):
        assert sites.resolve("site-a") == "site-a"

    def test_a_second_former_name_works_too(self, sites):
        assert sites.resolve("site-a-old") == "site-a"

    def test_a_site_that_never_changed_name_is_unaffected(self, sites):
        assert sites.resolve("site-b") == "site-b"

    def test_whitespace_is_not_a_different_site(self, sites):
        assert sites.resolve("  SITE-A-OLD  ") == "site-a"

    def test_every_name_a_site_has_had_can_be_listed(self, sites):
        """A migration needs to know what to look for, and a person
        auditing needs to know what they might find."""
        assert sites.aliases_of("site-a") == ("SITE-A-OLD", "site-a-old", "site-a")


class TestOneFormerNameCannotMeanTwoSites:
    def test_claiming_another_sites_alias_is_refused(self, sites):
        """Otherwise resolution order decides which dataset a reader gets,
        which is the ambiguity this exists to remove."""
        with pytest.raises(ValueError, match="cannot mean two sites"):
            sites.declare("somewhere-else", also_known_as=("SITE-A-OLD",))

    def test_redeclaring_the_same_pairing_is_fine(self, sites):
        sites.declare("site-a", also_known_as=("SITE-A-OLD",))
        assert sites.resolve("SITE-A-OLD") == "site-a"

    def test_a_blank_id_is_refused(self, sites):
        with pytest.raises(ValueError):
            sites.declare("   ")


class TestDeclarationIsSeparateFromResolution:
    def test_an_undeclared_id_passes_through_by_default(self, sites):
        """A deployment that has declared nothing has to keep working, and
        silently rewriting an id nobody has heard of would be worse than
        passing it through."""
        assert sites.resolve("brand-new-site") == "brand-new-site"

    def test_enforcement_refuses_it(self, sites):
        sites.require_declaration = True
        with pytest.raises(UnknownSite, match="brand-new-site"):
            sites.resolve("brand-new-site")

    def test_the_refusal_says_what_is_declared(self, sites):
        sites.require_declaration = True
        with pytest.raises(UnknownSite) as exc:
            sites.resolve("typo")
        assert "site-a" in str(exc.value)

    def test_the_refusal_says_how_a_rename_is_declared(self, sites):
        """The likeliest cause of an unknown id is a rename nobody
        recorded, so the message names the remedy."""
        sites.require_declaration = True
        with pytest.raises(UnknownSite, match="former name"):
            sites.resolve("typo")

    def test_a_blank_site_is_not_an_unknown_site(self, sites):
        """Nothing named is a different failure, and one the read path
        already refuses on its own terms."""
        sites.require_declaration = True
        assert sites.resolve("") == ""


class TestTheEnvironmentCanDeclareThem:
    def test_a_clause_per_site(self, monkeypatch):
        from axiom.infra import site_identity

        monkeypatch.setattr(site_identity, "_IDENTITIES", SiteIdentities())
        monkeypatch.setenv(
            "AXIOM_SITE_IDENTITIES",
            "site-a=SITE-A-OLD,site-a-old;site-d=site-d-old",
        )
        site_identity.load_from_env()
        assert site_identity.resolve("SITE-A-OLD") == "site-a"
        assert site_identity.resolve("site-d-old") == "site-d"

    def test_a_site_with_no_former_names(self, monkeypatch):
        from axiom.infra import site_identity

        monkeypatch.setattr(site_identity, "_IDENTITIES", SiteIdentities())
        monkeypatch.setenv("AXIOM_SITE_IDENTITIES", "site-b=")
        site_identity.load_from_env()
        assert site_identity.identities().known() == ("site-b",)

    def test_nothing_set_is_not_an_error(self, monkeypatch):
        from axiom.infra import site_identity

        monkeypatch.setattr(site_identity, "_IDENTITIES", SiteIdentities())
        monkeypatch.delenv("AXIOM_SITE_IDENTITIES", raising=False)
        site_identity.load_from_env()
        assert site_identity.identities().known() == ()

    def test_enforcement_is_opt_in(self, monkeypatch):
        from axiom.infra import site_identity

        monkeypatch.setattr(site_identity, "_IDENTITIES", SiteIdentities())
        monkeypatch.setenv("AXIOM_SITE_IDENTITIES", "site-b=")
        monkeypatch.setenv("AXIOM_SITE_REQUIRE_DECLARATION", "1")
        site_identity.load_from_env()
        with pytest.raises(UnknownSite):
            site_identity.resolve("other")


class TestTheIngestFaceLandsRowsUnderTheCanonicalId:
    @pytest.fixture(autouse=True)
    def declared(self, monkeypatch):
        from axiom.infra import site_identity

        registry = SiteIdentities()
        registry.declare("site-a", also_known_as=("SITE-A-OLD",))
        monkeypatch.setattr(site_identity, "_IDENTITIES", registry)
        return registry

    def _check(self, credential_site, payload=None):
        from axiom.extensions.builtins.data_platform.ingest_sink.tenancy import (
            SITE_KEY,
            IngestGrant,
            TenancyPolicy,
        )

        return TenancyPolicy().check(
            IngestGrant(principal="@x:y", site=credential_site, max_access_tier=None),
            payload or {},
        )[SITE_KEY]

    def test_a_credential_issued_before_the_rename_still_lands_canonical(self):
        """This is the fix. A credential outlives a rename, and stamping
        its id verbatim is how one site's readings end up in two places."""
        assert self._check("SITE-A-OLD") == "site-a"

    def test_a_producer_naming_the_old_id_is_not_a_foreign_tenant(self):
        """It is the same site. That is what an alias means, and refusing
        it would replace a silent fork with a silent outage."""
        assert self._check("site-a", {"site": "SITE-A-OLD"}) == "site-a"

    def test_a_genuinely_foreign_site_is_still_refused(self):
        from axiom.extensions.builtins.data_platform.ingest_sink.tenancy import (
            TenancyRefused,
        )

        with pytest.raises(TenancyRefused):
            self._check("site-a", {"site": "site-b"})


class TestAuthorisingASiteResolvesItFirst:
    def test_a_read_of_the_old_id_authorises_the_one_dataset(self, monkeypatch):
        """A rename that forked the authorisation would be the same
        failure wearing a different hat."""
        from axiom.infra import site_authority, site_identity

        registry = SiteIdentities()
        registry.declare("site-a", also_known_as=("SITE-A-OLD",))
        monkeypatch.setattr(site_identity, "_IDENTITIES", registry)
        # A named principal, because an unscoped read is refused on its own
        # terms and this is about WHICH site, not about who.
        assert (
            site_authority.authorize_site("SITE-A-OLD", principal="@reader:ut")
            == "site-a"
        )
