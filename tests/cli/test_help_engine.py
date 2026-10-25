# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the role+intent+tier help engine.

Per `prd-axi-cli.md §Progressive Disclosure` and the 2026-05-03 design
that distinguished competency from role: a command surfaces iff
(a) at least one of its intent_groups intersects the activated set
expanded from the user's roles, AND (b) its tier ≤ the user's
effective competency.  Reveal flags widen the surface deliberately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.cli import help_engine


def _cmd(noun: str, *, tier: str = "core", extension: str = "ext", **extras) -> dict:
    """Build a discover_cli_commands-shaped dict entry."""
    base = {
        "module": f"x.{noun}",
        "function": "main",
        "description": f"{noun} command",
        "extension": extension,
        "root": "/",
        "builtin": True,
        "tier": tier,
        "intent_groups": [],
        "verb_overrides": {},
    }
    base.update(extras)
    return base


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".axi"


# ---------------------------------------------------------------------------
# UserCompetency
# ---------------------------------------------------------------------------


class TestLoadCompetency:
    def test_missing_file_returns_starter_default(self, state_dir: Path) -> None:
        c = help_engine.load_competency(state_dir)
        assert c.global_tier == "starter"
        assert c.per_extension == {}

    def test_reads_global_tier(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True)
        (state_dir / "competency.json").write_text(json.dumps({"global": "advanced"}))
        c = help_engine.load_competency(state_dir)
        assert c.global_tier == "advanced"

    def test_reads_per_extension_overrides(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True)
        (state_dir / "competency.json").write_text(json.dumps({
            "global": "core",
            "per_extension": {"hygiene": "advanced", "research": "starter"},
        }))
        c = help_engine.load_competency(state_dir)
        assert c.global_tier == "core"
        assert c.per_extension == {"hygiene": "advanced", "research": "starter"}

    def test_invalid_tier_string_falls_back_to_default(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True)
        (state_dir / "competency.json").write_text(json.dumps({"global": "wizard"}))
        c = help_engine.load_competency(state_dir)
        assert c.global_tier == "starter"

    def test_corrupt_json_falls_back_silently(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True)
        (state_dir / "competency.json").write_text("not json {{")
        c = help_engine.load_competency(state_dir)
        assert c.global_tier == "starter"


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


class TestEffectiveTier:
    def test_no_override_returns_global(self) -> None:
        c = help_engine.UserCompetency(global_tier="advanced")
        assert c.effective_tier_for_extension("hygiene") == "advanced"

    def test_per_extension_lower_wins(self) -> None:
        """A new extension stays at starter even for a globally-advanced user."""
        c = help_engine.UserCompetency(
            global_tier="advanced",
            per_extension={"new-ext": "starter"},
        )
        assert c.effective_tier_for_extension("new-ext") == "starter"

    def test_per_extension_higher_capped_by_global(self) -> None:
        """Per-extension can't exceed global — the user's competency
        ceiling wins. A user globally at `core` doesn't suddenly become
        an `advanced` operator just because they've used one extension a lot."""
        c = help_engine.UserCompetency(
            global_tier="core",
            per_extension={"hygiene": "internal"},
        )
        assert c.effective_tier_for_extension("hygiene") == "core"


# ---------------------------------------------------------------------------
# cmd_meets_tier
# ---------------------------------------------------------------------------


class TestCmdMeetsTier:
    @pytest.mark.parametrize("user_tier,cmd_tier,expected", [
        # User at starter sees starter only.
        ("starter", "starter", True),
        ("starter", "core", False),
        ("starter", "advanced", False),
        # User at core sees starter + core.
        ("core", "starter", True),
        ("core", "core", True),
        ("core", "advanced", False),
        # User at advanced sees through advanced.
        ("advanced", "core", True),
        ("advanced", "advanced", True),
        ("advanced", "internal", False),
        # User at internal sees everything.
        ("internal", "internal", True),
    ])
    def test_inclusion(self, user_tier: str, cmd_tier: str, expected: bool) -> None:
        info = _cmd("x", tier=cmd_tier)
        assert help_engine.cmd_meets_tier(info, user_tier) is expected

    def test_undeclared_tier_defaults_to_core(self) -> None:
        """An old manifest without tier= surfaces at 'core' by default,
        so existing extensions keep working unchanged."""
        info = {"description": "x", "extension": "old", "tier": None}
        assert help_engine.cmd_meets_tier(info, "core") is True
        assert help_engine.cmd_meets_tier(info, "starter") is False


# ---------------------------------------------------------------------------
# filter_commands
# ---------------------------------------------------------------------------


class TestFilterCommands:
    def test_starter_user_sees_only_starter_cmds(self) -> None:
        cmds = {
            "chat": _cmd("chat", tier="starter"),
            "tidy": _cmd("tidy", tier="core"),
            "rag": _cmd("rag", tier="advanced"),
        }
        out = help_engine.filter_commands(
            cmds, user_competency=help_engine.UserCompetency(global_tier="starter"),
        )
        assert set(out) == {"chat"}

    def test_core_user_sees_starter_and_core(self) -> None:
        cmds = {
            "chat": _cmd("chat", tier="starter"),
            "tidy": _cmd("tidy", tier="core"),
            "rag": _cmd("rag", tier="advanced"),
        }
        out = help_engine.filter_commands(
            cmds, user_competency=help_engine.UserCompetency(global_tier="core"),
        )
        assert set(out) == {"chat", "tidy"}

    def test_show_all_widens_to_advanced_but_hides_internal(self) -> None:
        cmds = {
            "chat": _cmd("chat", tier="starter"),
            "rag": _cmd("rag", tier="advanced"),
            "debug": _cmd("debug", tier="internal"),
        }
        out = help_engine.filter_commands(
            cmds, show_all=True,
            user_competency=help_engine.UserCompetency(global_tier="starter"),
        )
        assert set(out) == {"chat", "rag"}

    def test_include_internal_required_for_internal_cmds(self) -> None:
        cmds = {"debug": _cmd("debug", tier="internal")}
        without = help_engine.filter_commands(
            cmds, show_all=True,
            user_competency=help_engine.UserCompetency(global_tier="internal"),
        )
        assert without == {}
        with_flag = help_engine.filter_commands(
            cmds, show_all=True, include_internal=True,
            user_competency=help_engine.UserCompetency(global_tier="internal"),
        )
        assert set(with_flag) == {"debug"}

    def test_tier_override_wins_over_user(self) -> None:
        cmds = {
            "chat": _cmd("chat", tier="starter"),
            "rag": _cmd("rag", tier="advanced"),
        }
        out = help_engine.filter_commands(
            cmds,
            user_competency=help_engine.UserCompetency(global_tier="starter"),
            tier_override="advanced",
        )
        assert set(out) == {"chat", "rag"}

    def test_intent_group_filter(self) -> None:
        cmds = {
            "chat": _cmd("chat", tier="starter", intent_groups=["start"]),
            "tidy": _cmd("tidy", tier="core", intent_groups=["maintain"]),
            "rivet": _cmd("rivet", tier="core", intent_groups=["maintain", "build"]),
        }
        out = help_engine.filter_commands(
            cmds, intent_group="maintain",
            user_competency=help_engine.UserCompetency(global_tier="core"),
        )
        assert set(out) == {"tidy", "rivet"}

    def test_per_extension_competency_keeps_new_ext_quiet(self) -> None:
        """An advanced-globally user sees a brand-new extension at starter."""
        cmds = {
            "old-advanced": _cmd("old", tier="advanced", extension="old"),
            "new-advanced": _cmd("new", tier="advanced", extension="new"),
        }
        c = help_engine.UserCompetency(
            global_tier="advanced",
            per_extension={"new": "starter"},
        )
        out = help_engine.filter_commands(cmds, user_competency=c)
        assert "old-advanced" in out
        assert "new-advanced" not in out  # gated to starter for `new` ext


# ---------------------------------------------------------------------------
# group_by_intent
# ---------------------------------------------------------------------------


class TestGroupByIntent:
    def test_groups_commands_by_intent(self) -> None:
        cmds = {
            "chat": _cmd("chat", intent_groups=["start"]),
            "doctor": _cmd("doctor", intent_groups=["start", "investigate"]),
            "tidy": _cmd("tidy", intent_groups=["maintain"]),
        }
        groups = help_engine.group_by_intent(cmds)
        assert groups["start"] == ["chat", "doctor"]
        assert groups["investigate"] == ["doctor"]
        assert groups["maintain"] == ["tidy"]


# ---------------------------------------------------------------------------
# AXI_HELP_FLAT escape hatch
# ---------------------------------------------------------------------------


class TestQuietEscape:
    def test_axi_help_flat_disables_filtering(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AXI_HELP_FLAT", "1")
        assert help_engine.is_quiet() is True

    def test_unset_means_filtered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AXI_HELP_FLAT", raising=False)
        assert help_engine.is_quiet() is False


# ---------------------------------------------------------------------------
# Role + intent expansion (the 2026-05-03 design)
# ---------------------------------------------------------------------------


class TestRoleIntentExpansion:
    def test_basic_role_activates_only_start(self) -> None:
        c = help_engine.UserCompetency(roles=("basic",))
        assert c.expand_intents() == frozenset({"start"})

    def test_researcher_activates_start_research_investigate(self) -> None:
        c = help_engine.UserCompetency(roles=("researcher",))
        assert c.expand_intents() == frozenset({"start", "research", "investigate"})

    def test_multi_role_unions_intents(self) -> None:
        c = help_engine.UserCompetency(roles=("researcher", "instructor"))
        assert c.expand_intents() == frozenset({
            "start", "research", "teach", "investigate"
        })

    def test_unknown_role_falls_back_to_start_floor(self) -> None:
        """A typo or removed role can't leave the user with zero visible
        commands — `start` is always the floor."""
        c = help_engine.UserCompetency(roles=("nonexistent",))
        # Engine ignores unknown roles when loading from JSON, but a
        # programmatically-constructed competency could still hit this.
        assert "start" in c.expand_intents()


class TestRoleIntentFiltering:
    def test_basic_user_sees_only_start_intent_commands(self) -> None:
        cmds = {
            "chat": _cmd("chat", intent_groups=["start"]),
            "research": _cmd("research", intent_groups=["research"]),
            "ext": _cmd("ext", intent_groups=["build"]),
        }
        out = help_engine.filter_commands(
            cmds,
            user_competency=help_engine.UserCompetency(
                roles=("basic",), global_tier="core"),
        )
        assert set(out) == {"chat"}

    def test_researcher_sees_research_and_start(self) -> None:
        cmds = {
            "chat": _cmd("chat", intent_groups=["start"]),
            "research": _cmd("research", intent_groups=["research"]),
            "classroom": _cmd("classroom", intent_groups=["teach"]),
        }
        out = help_engine.filter_commands(
            cmds,
            user_competency=help_engine.UserCompetency(
                roles=("researcher",), global_tier="core"),
        )
        assert set(out) == {"chat", "research"}

    def test_builder_sees_build_and_maintain_and_start(self) -> None:
        cmds = {
            "chat": _cmd("chat", intent_groups=["start"]),
            "ext": _cmd("ext", intent_groups=["build"]),
            "tidy": _cmd("tidy", intent_groups=["maintain"]),
            "research": _cmd("research", intent_groups=["research"]),
        }
        out = help_engine.filter_commands(
            cmds,
            user_competency=help_engine.UserCompetency(
                roles=("builder",), global_tier="core"),
        )
        assert set(out) == {"chat", "ext", "tidy"}

    def test_undeclared_intents_match_universally(self) -> None:
        """A command with empty intent_groups is universal fallback —
        surfaces for every role.  Renderer puts it under 'Other:'."""
        cmds = {
            "ghost": _cmd("ghost", intent_groups=[], tier="core"),
        }
        for roles in (("basic",), ("researcher",), ("builder",)):
            out = help_engine.filter_commands(
                cmds,
                user_competency=help_engine.UserCompetency(
                    roles=roles, global_tier="core"),
            )
            assert "ghost" in out, f"undeclared cmd should surface for {roles}"

    def test_role_override_peeks_at_other_role(self) -> None:
        """`axi --role builder` lets a researcher see the builder
        surface for one command without persisting role membership."""
        cmds = {
            "research": _cmd("research", intent_groups=["research"]),
            "ext": _cmd("ext", intent_groups=["build"]),
        }
        out = help_engine.filter_commands(
            cmds,
            user_competency=help_engine.UserCompetency(
                roles=("researcher",), global_tier="core"),
            role_override=("builder",),
        )
        assert set(out) == {"ext"}  # builder activated, researcher's intents replaced

    def test_show_all_bypasses_role_filtering(self) -> None:
        """`--all` widens past role filtering (still hides internal)."""
        cmds = {
            "chat": _cmd("chat", intent_groups=["start"]),
            "ext": _cmd("ext", intent_groups=["build"]),
            "research": _cmd("research", intent_groups=["research"]),
        }
        out = help_engine.filter_commands(
            cmds, show_all=True,
            user_competency=help_engine.UserCompetency(
                roles=("basic",), global_tier="advanced"),
        )
        assert set(out) == {"chat", "ext", "research"}


class TestPersistence:
    def test_roundtrip_preserves_roles(self, state_dir: Path) -> None:
        original = help_engine.UserCompetency(
            roles=("researcher", "builder"),
            global_tier="core",
            per_extension={"hygiene": "advanced"},
        )
        help_engine.save_competency(original, state_dir)
        loaded = help_engine.load_competency(state_dir)
        assert loaded.roles == ("researcher", "builder")
        assert loaded.global_tier == "core"
        assert loaded.per_extension == {"hygiene": "advanced"}

    def test_unknown_role_in_json_is_dropped(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True)
        (state_dir / "competency.json").write_text(json.dumps({
            "roles": ["researcher", "wizard", "builder"],
            "global": "core",
        }))
        c = help_engine.load_competency(state_dir)
        assert c.roles == ("researcher", "builder")

    def test_empty_role_list_falls_back_to_basic(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True)
        (state_dir / "competency.json").write_text(json.dumps({
            "roles": [], "global": "core"
        }))
        c = help_engine.load_competency(state_dir)
        assert c.roles == ("basic",)
