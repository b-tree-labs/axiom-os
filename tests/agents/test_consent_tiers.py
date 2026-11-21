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


class TestAnAgentShippedLaterWasNeverDeclined:
    """A decision covers what was on the table.

    `load_consent` read a stored `enabled` list verbatim, so an agent that
    reached the core tier AFTER the operator decided was never enabled and
    nothing said so. Measured on a real machine 2026-10-02: decided at
    0.47.0 with `hygiene` and `release`; `diagnostics`, `directory` and
    `vault` had become core since, and none had ever run — which is why a
    credential sweep that was shipped, scheduled and wired had not executed
    in ten days.

    Treating that silence as refusal reads a choice the operator did not
    make. It is the same error as treating a site that has declared nothing
    as having declared the floor.
    """

    def _write(self, tmp_path, monkeypatch, record):
        import json

        from axiom.extensions.builtins.agents import consent as mod

        path = tmp_path / "agents_consent.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        monkeypatch.setattr(mod, "consent_path", lambda: path)
        monkeypatch.setattr(mod, "default_enabled_agents", lambda *a, **k: ["hygiene", "vault"])
        return mod

    def test_a_record_that_knows_what_was_offered_adopts_the_new_core_agent(
        self, tmp_path, monkeypatch
    ):
        mod = self._write(tmp_path, monkeypatch, {
            "decided": True, "enabled": ["hygiene"], "known": ["hygiene", "release"],
        })
        assert "vault" in mod.load_consent().enabled

    def test_it_does_not_re_enable_something_that_was_declined(
        self, tmp_path, monkeypatch
    ):
        """`release` was on the table and not chosen. That is a decision."""
        mod = self._write(tmp_path, monkeypatch, {
            "decided": True, "enabled": ["hygiene"], "known": ["hygiene", "release"],
        })
        assert "release" not in mod.load_consent().enabled

    def test_a_legacy_record_is_not_changed_behind_the_operator(
        self, tmp_path, monkeypatch
    ):
        """Without `known`, "declined" and "shipped later" are
        indistinguishable, and the platform will not guess in the direction
        that starts running something."""
        mod = self._write(tmp_path, monkeypatch, {
            "decided": True, "enabled": ["hygiene"], "decided_version": "0.47.0",
        })
        assert mod.load_consent().enabled == ["hygiene"]

    def test_but_a_legacy_record_reports_what_it_never_saw(self, tmp_path, monkeypatch):
        """...and it does not stay quiet either. Quiet is the failure."""
        mod = self._write(tmp_path, monkeypatch, {
            "decided": True, "enabled": ["hygiene"], "decided_version": "0.47.0",
        })
        assert mod.never_offered(mod.load_consent()) == ["vault"]

    def test_opting_out_is_a_real_never(self, tmp_path, monkeypatch):
        """An operator who said no to everything is not re-asked by this."""
        mod = self._write(tmp_path, monkeypatch, {
            "decided": True, "opted_out": True, "enabled": [],
        })
        assert mod.load_consent().enabled == []
        assert mod.never_offered(mod.load_consent()) == []

    def test_a_decided_record_now_records_what_was_offered(self, tmp_path, monkeypatch):
        """So the next agent to ship does not land in the same ambiguity."""
        import json

        from axiom.extensions.builtins.agents import consent as mod

        path = tmp_path / "agents_consent.json"
        monkeypatch.setattr(mod, "consent_path", lambda: path)
        mod.save_consent(mod.AgentConsent(decided=True, enabled=["hygiene"]))
        assert json.loads(path.read_text(encoding="utf-8"))["known"]


class TestAConsentChangeLeavesATrace:
    """"Why did this agent stop?" had no answer anywhere.

    The consent file holds only the CURRENT decision. On a real machine
    `vault` and `diagnostics` stopped being dispatched on 2026-09-21 and
    `directory` and `publishing` on 2026-09-07; the tick log proved they
    had stopped and was silent on the cause, and the consent file had been
    rewritten since. Nothing could say what changed on either date.

    A reader with no answer invents one — in this case "they never ran",
    asserted about agents the same table showed had run 110 times.
    """

    def _mod(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.agents import consent as mod

        monkeypatch.setattr(mod, "consent_path", lambda: tmp_path / "c.json")
        monkeypatch.setattr(mod, "_all_agent_names", lambda *a, **k: ["hygiene", "vault"])
        monkeypatch.setattr(mod, "default_enabled_agents", lambda *a, **k: ["hygiene", "vault"])
        return mod

    def test_losing_an_agent_is_recorded(self, tmp_path, monkeypatch):
        import json

        mod = self._mod(tmp_path, monkeypatch)
        mod.record_decision(enabled=["hygiene", "vault"], version="1.0")
        mod.record_decision(enabled=["hygiene"], version="1.1")
        rows = [json.loads(x) for x in mod.journal_path().read_text().splitlines()]
        assert rows[-1]["lost"] == ["vault"]
        assert rows[-1]["version"] == "1.1"

    def test_gaining_one_is_recorded_too(self, tmp_path, monkeypatch):
        import json

        mod = self._mod(tmp_path, monkeypatch)
        mod.record_decision(enabled=["hygiene"], version="1.0")
        mod.record_decision(enabled=["hygiene", "vault"], version="1.1")
        rows = [json.loads(x) for x in mod.journal_path().read_text().splitlines()]
        assert rows[-1]["gained"] == ["vault"]

    def test_a_decision_that_changes_nothing_writes_nothing(self, tmp_path, monkeypatch):
        """A journal full of no-ops is one nobody reads."""
        mod = self._mod(tmp_path, monkeypatch)
        mod.record_decision(enabled=["hygiene"], version="1.0")
        before = mod.journal_path().read_text()
        mod.record_decision(enabled=["hygiene"], version="1.0")
        assert mod.journal_path().read_text() == before

    def test_it_is_append_only(self, tmp_path, monkeypatch):
        mod = self._mod(tmp_path, monkeypatch)
        for n, e in enumerate([["hygiene"], ["hygiene", "vault"], ["vault"]]):
            mod.record_decision(enabled=e, version=f"1.{n}")
        assert len(mod.journal_path().read_text().strip().splitlines()) == 3

    def test_a_journal_that_cannot_be_written_does_not_block_the_decision(
        self, tmp_path, monkeypatch
    ):
        """Losing the trace must not stop an operator deciding."""
        mod = self._mod(tmp_path, monkeypatch)
        monkeypatch.setattr(mod, "journal_path", lambda: tmp_path / "nope" / "x.jsonl")
        got = mod.record_decision(enabled=["hygiene"], version="1.0")
        assert got.enabled == ["hygiene"]
