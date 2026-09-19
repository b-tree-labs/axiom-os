# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the daemon-agent consent gate (2026-05-28 silent-install incident).

Host-persistent service registration must never happen without explicit
operator consent; this gate records the one-time decision (all / none / subset)
and is what startup self-heal consults so it never surprises the operator.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.agents import consent as C


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    # Both, deliberately. `consent_path` reads the PLATFORM directory now, so
    # patching only the branded one let these tests fall through to the real
    # ~/.axi and assert against the operator's own consent record.
    monkeypatch.setattr(C, "get_user_state_dir", lambda: tmp_path)
    monkeypatch.setattr("axiom.infra.paths.get_platform_state_dir", lambda: tmp_path)
    return tmp_path


class TestLoadSave:
    def test_missing_file_is_undecided(self, state_dir):
        c = C.load_consent()
        assert c.decided is False
        assert c.opted_out is False
        assert c.enabled == []

    def test_round_trip(self, state_dir):
        C.save_consent(C.AgentConsent(decided=True, opted_out=False,
                                      enabled=["diagnostics", "hygiene"]))
        c = C.load_consent()
        assert c.decided is True
        assert c.enabled == ["diagnostics", "hygiene"]

    def test_corrupt_file_reads_undecided(self, state_dir):
        C.consent_path().write_text("{not json", encoding="utf-8")
        assert C.load_consent().decided is False


class TestRecordDecision:
    def test_enable_subset(self, state_dir):
        c = C.record_decision(enabled=["diagnostics"])
        assert c.decided is True and c.opted_out is False
        assert C.load_consent().enabled == ["diagnostics"]

    def test_opt_out(self, state_dir):
        c = C.record_decision(enabled=[], opted_out=True)
        assert c.decided is True and c.opted_out is True
        assert C.load_consent().opted_out is True


class TestSelfHealGate:
    def test_undecided_self_heals_nothing(self):
        # No prior consent → never (re)install on startup without a prompt.
        c = C.AgentConsent()
        assert C.agents_to_self_heal(c, ["diagnostics", "hygiene"]) == []
        assert C.needs_prompt(c, ["diagnostics", "hygiene"]) is True

    def test_opted_out_self_heals_nothing_and_no_nag(self):
        c = C.AgentConsent(decided=True, opted_out=True)
        assert C.agents_to_self_heal(c, ["diagnostics"]) == []
        assert C.needs_prompt(c, ["diagnostics"]) is False  # decided → no re-nag

    def test_approved_subset_self_heals_only_intersection(self):
        c = C.AgentConsent(decided=True, enabled=["diagnostics", "hygiene"])
        assert C.agents_to_self_heal(c, ["hygiene", "release"]) == ["hygiene"]
        assert C.needs_prompt(c, ["hygiene", "release"]) is False

    def test_no_missing_no_prompt(self):
        assert C.needs_prompt(C.AgentConsent(), []) is False


class TestParseRegisterSelection:
    CANDS = ["diagnostics", "hygiene", "release"]

    def test_all(self):
        assert C.parse_register_selection("a", self.CANDS) == (self.CANDS, False)
        assert C.parse_register_selection("ALL", self.CANDS) == (self.CANDS, False)

    def test_none_opts_out(self):
        assert C.parse_register_selection("none", self.CANDS) == ([], True)
        assert C.parse_register_selection("n", self.CANDS) == ([], True)

    def test_numeric_picks_1_based_dedup_order_preserved(self):
        assert C.parse_register_selection("1,3", self.CANDS) == (
            ["diagnostics", "release"], False)
        assert C.parse_register_selection("3 1 3", self.CANDS) == (
            ["release", "diagnostics"], False)

    @pytest.mark.parametrize("bad", ["", "  ", "0", "9", "x", "1,x"])
    def test_invalid_raises(self, bad):
        with pytest.raises(ValueError):
            C.parse_register_selection(bad, self.CANDS)


class TestReofferAfterOptout:
    def test_records_version(self, state_dir, monkeypatch):
        monkeypatch.setattr(C, "current_version", lambda: "0.22.0")
        c = C.record_decision(enabled=[], opted_out=True)
        assert c.decided_version == "0.22.0"
        assert C.load_consent().decided_version == "0.22.0"

    def test_reoffer_on_minor_bump(self):
        c = C.AgentConsent(decided=True, opted_out=True, decided_version="0.22.0")
        assert C.should_reoffer_after_optout(c, "0.23.0") is True

    def test_reoffer_on_major_bump(self):
        c = C.AgentConsent(decided=True, opted_out=True, decided_version="0.22.0")
        assert C.should_reoffer_after_optout(c, "1.0.0") is True

    def test_no_reoffer_on_patch_bump(self):
        c = C.AgentConsent(decided=True, opted_out=True, decided_version="0.22.0")
        assert C.should_reoffer_after_optout(c, "0.22.5") is False

    def test_no_reoffer_when_not_opted_out(self):
        c = C.AgentConsent(decided=True, opted_out=False, enabled=["x"],
                           decided_version="0.22.0")
        assert C.should_reoffer_after_optout(c, "0.99.0") is False

    @pytest.mark.parametrize("prev,cur", [("", "0.23.0"), ("0.22.0", ""), ("x", "y")])
    def test_unparseable_versions_stay_quiet(self, prev, cur):
        c = C.AgentConsent(decided=True, opted_out=True, decided_version=prev)
        assert C.should_reoffer_after_optout(c, cur) is False


@pytest.fixture(autouse=True)
def _restore_branding():
    """Registering branding appends to a module-level list that nothing pops.

    These tests deliberately switch brand, and without this every test that
    ran AFTER them saw `neut` — which is how they broke two unrelated
    diagnostics tests that assert on `.axi` paths. Passing alone and failing
    in a suite is the signature.
    """
    from axiom.infra import branding

    saved = list(branding._registered)
    try:
        yield
    finally:
        branding._registered[:] = saved


class TestConsentIsNotSplitByBrand:
    """`neut agents status` said "opted out (no agents run)" while six agents
    were dispatching under consent granted via `axi`.

    There is one Background Service per machine and it runs as the platform,
    so the consent it reads is the platform's. A brand-scoped record can only
    disagree with what is actually running.
    """

    def test_the_path_does_not_move_with_the_brand(self, monkeypatch, tmp_path):
        from pathlib import Path

        from axiom.extensions.builtins.agents import consent
        from axiom.infra import branding

        monkeypatch.delenv("AXI_STATE_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        under_axi = consent.consent_path()
        branding.register(
            branding.BrandingConfig(
                cli_name="acme", product_name="Acme OS", package_name="acme-os"
            )
        )
        assert consent.consent_path() == under_axi, (
            f"consent moved with the brand: {consent.consent_path()}"
        )
        assert ".acme" not in str(consent.consent_path())

    def test_a_decision_recorded_under_a_brand_is_adopted(self, monkeypatch, tmp_path):
        """Otherwise the operator is asked again for a decision they made."""
        import json
        from pathlib import Path

        from axiom.extensions.builtins.agents import consent
        from axiom.infra import branding

        monkeypatch.delenv("AXI_STATE_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        branding.register(
            branding.BrandingConfig(
                cli_name="acme", product_name="Acme OS", package_name="acme-os"
            )
        )
        branded = tmp_path / ".acme"
        branded.mkdir(parents=True, exist_ok=True)
        (branded / consent._CONSENT_FILE).write_text(
            json.dumps({"decided": True, "opted_out": False, "enabled": ["hygiene"]})
        )

        resolved = consent.consent_path()
        assert resolved.exists(), "the existing decision was not adopted"
        assert json.loads(resolved.read_text())["enabled"] == ["hygiene"]

    def test_an_existing_shared_record_is_not_overwritten(self, monkeypatch, tmp_path):
        import json
        from pathlib import Path

        from axiom.extensions.builtins.agents import consent
        from axiom.infra import branding

        monkeypatch.delenv("AXI_STATE_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        shared = tmp_path / ".axi"
        shared.mkdir(parents=True, exist_ok=True)
        (shared / consent._CONSENT_FILE).write_text(json.dumps({"enabled": ["kept"]}))
        branding.register(
            branding.BrandingConfig(
                cli_name="acme", product_name="Acme OS", package_name="acme-os"
            )
        )
        branded = tmp_path / ".acme"
        branded.mkdir(parents=True, exist_ok=True)
        (branded / consent._CONSENT_FILE).write_text(json.dumps({"enabled": ["stale"]}))

        assert json.loads(consent.consent_path().read_text())["enabled"] == ["kept"]
