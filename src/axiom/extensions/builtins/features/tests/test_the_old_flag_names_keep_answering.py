# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The old names keep answering for one release (ADR-254 as amended 2026-11-12).

``flags`` is a real noun again; the flag tools that existed only as
``axiom_features__<verb>`` answer under that name, deprecated, until 0.71.0, and
the ``flags/registry.py`` / ``flags/service.py`` import paths stay until then.
"""

from __future__ import annotations

from axiom.extensions.builtins.features.tests.conftest import mcp


def test_the_mcp_tools_answer_under_both_names_whichever_extension_binds(switchboard):
    from axiom.extensions.builtins.features.skills import bind as features_bind
    from axiom.extensions.builtins.flags.skills import bind as flags_bind

    state = switchboard["state"]
    for bind in (features_bind, flags_bind):
        out = mcp("axiom_flags__set", {"key": "timeline", "value": "on", "scope": "account", "scope_id": "acct"},
                  state, bind=bind)
        assert out["ok"], out
        out = mcp("axiom_flags__set", {"key": "timeline", "value": "soon", "scope": "user", "scope_id": "u1"},
                  state, bind=bind)
        assert out["ok"], out
        for tool in ("axiom_flags__resolve", "axiom_features__resolve"):
            resolved = mcp(tool, {"key": "timeline", "account": "acct", "user": "u1"}, state, bind=bind)
            assert resolved["value"]["flags"] == {"timeline": "soon"}, tool
        listed = mcp("axiom_flags__list", {}, state, bind=bind)["value"]
        assert listed["flags"][0]["key"] == "library"  # the shape axiom_flags__list always had
        assert {r["name"] for r in listed["rows"]} == {"library", "panel", "timeline"}


def test_the_moved_modules_stay_importable_at_their_old_paths():
    from axiom.extensions.builtins.features import flag_registry, resolver
    from axiom.extensions.builtins.flags import registry, service

    assert registry.FlagDef is flag_registry.FlagDef and registry.set_registry is flag_registry.set_registry
    assert service.FlagService is resolver.FlagService and service.env_override is resolver.env_override


def test_only_the_deprecated_names_say_when_they_go():
    from axiom.extensions.builtins.features.skills import bind
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    for name in ("flags.list", "flags.resolve", "flags.set", "flags.kill"):
        assert "0.71.0" not in reg.spec(name).description, name
    for name in ("features.resolve", "features.kill", "features.cohort_set"):
        assert "removed at 0.71.0" in reg.spec(name).description, name
