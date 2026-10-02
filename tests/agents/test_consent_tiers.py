# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A fresh install runs a core set of agents, not none of them.

THE DEFECT: `AgentConsent.enabled` defaulted to `field(default_factory=list)`
— an empty list. A new install therefore dispatched nothing, and the operator
had to discover `axi agents register` to get any value at all. Measured on a
real machine: six of seven agents had never run, one of them (release/RIVET)
for 23 days, which is why sixteen consecutive CI failures on main went unseen.

Net-zero is a bad default. So is net-everything: three of the seven reach
outside the machine (GitHub, OneDrive, an inbox, a publishing target), and
turning those on without asking would be a surprise the operator did not buy.

The line is drawn in the MANIFEST, so it is auditable before install rather
than decided by a table in this file: an agent may default on when it reads
only local state and writes only its own.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from axiom.extensions.builtins.agents.consent import CORE_TIER, default_enabled_agents

_BUILTINS = Path(__file__).parents[2] / "src" / "axiom" / "extensions" / "builtins"


def _manifests():
    for f in sorted(_BUILTINS.glob("*/axiom-extension.toml")):
        data = tomllib.loads(f.read_text(encoding="utf-8"))
        if "agent" in data.get("extension", {}) or "agent" in data:
            yield f.parent.name, data


def _agent_block(data):
    return data.get("agent") or data.get("extension", {}).get("agent") or {}


class TestTheDefaultIsNotEmpty:
    def test_a_fresh_install_enables_something(self):
        assert default_enabled_agents(), "net-zero agents is the defect this fixes"

    def test_the_core_set_is_what_a_developer_needs(self):
        """Local hygiene, local failure triage, local credential watch."""
        core = set(default_enabled_agents())
        assert {"hygiene", "diagnostics", "vault"} <= core


class TestWhatIsNotCore:
    """An agent that reaches outside the machine is a consent decision."""

    @pytest.mark.parametrize("name", ["signals", "publishing"])
    def test_outbound_agents_are_not_on_by_default(self, name):
        assert name not in default_enabled_agents()


class TestTheTierIsDeclaredNotInferred:
    def test_every_agent_declares_its_tier(self):
        """A tier decided here rather than in the manifest is a tier the
        operator cannot audit before installing."""
        missing = [
            name for name, data in _manifests()
            if not _agent_block(data).get("default_consent")
        ]
        assert not missing, f"these declare [agent] but no default_consent: {missing}"

    def test_a_declared_tier_is_one_we_recognise(self):
        bad = [
            (name, _agent_block(data).get("default_consent"))
            for name, data in _manifests()
            if _agent_block(data).get("default_consent") not in {CORE_TIER, "opt-in"}
        ]
        assert not bad, f"unrecognised tiers: {bad}"

    def test_the_default_set_is_exactly_what_the_manifests_declare(self):
        """No second source of truth: change the manifest, change the default."""
        declared = {
            name for name, data in _manifests()
            if _agent_block(data).get("default_consent") == CORE_TIER
        }
        assert set(default_enabled_agents()) == declared


class TestTheDefaultIsActuallyUsed:
    """The wiring check. A `default_enabled_agents()` nothing calls is the same
    bug as a `delegate_to_agent` that resolves and returns."""

    def test_a_machine_with_no_consent_file_gets_the_core_set(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.agents import consent as mod

        monkeypatch.setattr(mod, "consent_path", lambda: tmp_path / "nope.json")
        assert set(mod.load_consent().enabled) == set(default_enabled_agents())

    def test_an_operator_who_said_no_to_everything_keeps_that(self, tmp_path, monkeypatch):
        """"Never asked" and "said no" were the same state before this. An
        explicit empty list is a decision and must survive a reload."""
        import json

        from axiom.extensions.builtins.agents import consent as mod

        f = tmp_path / "c.json"
        f.write_text(json.dumps({"decided": True, "opted_out": False, "enabled": []}))
        monkeypatch.setattr(mod, "consent_path", lambda: f)
        assert mod.load_consent().enabled == []

    def test_an_existing_choice_is_not_widened(self, tmp_path, monkeypatch):
        import json

        from axiom.extensions.builtins.agents import consent as mod

        f = tmp_path / "c.json"
        f.write_text(json.dumps({"decided": True, "enabled": ["hygiene"]}))
        monkeypatch.setattr(mod, "consent_path", lambda: f)
        assert mod.load_consent().enabled == ["hygiene"]
