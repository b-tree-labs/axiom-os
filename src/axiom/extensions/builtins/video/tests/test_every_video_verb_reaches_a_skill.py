# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every ``axi instrument video`` verb is a thin wrapper over skill ``video.<verb>`` (ADR-056).

The skills are named for the video extension, not for the noun (ADR-256 D5).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from axiom.extensions.builtins.video import skills
from axiom.extensions.builtins.video.commands import cli
from axiom.infra.skills import SkillRegistry

MANIFEST = Path(skills.__file__).parents[1] / "axiom-extension.toml"
VERBS = (
    "add",
    "verify",
    "list",
    "status",
    "start",
    "stop",
    "diagnose",
    "install",
    "uninstall",
    "revoke",
    "events",
)


def _registered() -> set[str]:
    registry = SkillRegistry()
    skills.bind(registry)
    return set(registry.list())


def _declared() -> set[str]:
    return set(
        re.findall(r'^name = "(video\.[a-z_]+)"$', MANIFEST.read_text(encoding="utf-8"), re.M)
    )


def test_the_verbs_are_exactly_the_cli():
    """Phase 1's verbs, then phase 2's one: ``events``. A trigger is attached by
    configuration and ``connector reload``, not by a verb (VID-015)."""
    assert tuple(cli.VERBS) == VERBS
    assert not {"attach", "detach", "trigger", "triggers"} & set(cli.VERBS)


def test_every_verb_resolves_to_a_registered_and_declared_skill():
    for verb in cli.VERBS:
        assert cli.skill_for(verb) == f"video.{verb}"
    assert {cli.skill_for(v) for v in cli.VERBS} <= _registered()
    assert {cli.skill_for(v) for v in cli.VERBS} <= _declared()


def test_the_instrument_kind_offers_exactly_these_verbs_and_skills():
    from axiom.extensions.builtins.video.instrument import KIND

    assert dict(KIND.skill_for_verb) == {verb: cli.skill_for(verb) for verb in cli.VERBS}


def test_the_guard_can_fail():
    assert "video.record" not in _registered(), "recording is out of scope (prd-video §5)"


def test_every_skill_declares_its_risk_and_the_writes_their_preconditions():
    registry = SkillRegistry()
    skills.bind(registry)
    for name, spec in registry.specs().items():
        assert spec.risk in ("read", "reversible-write", "destructive"), name
        if spec.risk != "read":
            assert spec.requires is not None, name


def test_reads_are_reads():
    registry = SkillRegistry()
    skills.bind(registry)
    assert {n for n, s in registry.specs().items() if s.risk == "read"} == {
        "video.list",
        "video.status",
        "video.diagnose",
        "video.events",
    }


def test_start_requires_a_verified_source_and_ffmpeg():
    registry = SkillRegistry()
    skills.bind(registry)
    ids = {p.id for p in registry.spec("video.start").requires}
    assert {"source-declared", "source-verified", "ffmpeg-available"} <= ids


def test_add_offers_the_flags_the_kinds_register():
    help_text = cli.get_parser().format_help() + _subparser_help("add")
    for flag in ("--kind", "--url", "--credential-ref", "--transport", "--fps-cap"):
        assert flag in help_text, flag


def _subparser_help(verb: str) -> str:
    parser = cli.get_parser()
    for action in parser._actions:  # noqa: SLF001 - argparse has no public accessor
        if getattr(action, "choices", None) and verb in action.choices:
            return action.choices[verb].format_help()
    return ""


def test_the_manifest_is_valid_toml():
    tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
