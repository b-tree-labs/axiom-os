# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A node given a role shows only that role's commands, and nothing is lost.

A partner installing a data-acquisition collector met the whole platform: dozens
of commands, background agents offering to install themselves, and no way to
tell which parts were theirs. ADR-164 says a node declares its functions; this
is the half of that a person sees. With a role applied, help and dispatch show
the role's commands plus the few every node needs, background agents stay off,
and anything else can be turned on later from config, with no reinstall.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.infra import node_functions as nf

COMMANDS = {
    "daq": {"functions": ["acquire", "transmit"], "description": "collector"},
    "site": {"functions": ["transmit"], "description": "binding"},
    "chat": {"functions": ["assistant"], "description": "chat"},
    "data": {"functions": ["data_platform"], "description": "platform"},
    "bench": {"functions": [], "description": "a command no function owns"},
    "whoami": {"functions": [], "description": "who am I"},
    "status": {"functions": [], "description": "status"},
}


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "node.toml"
    monkeypatch.setenv(nf.CONFIG_ENV, str(path))
    return path


def test_an_unconfigured_node_shows_everything(cfg_path):
    cfg = nf.load()
    assert not cfg.confined
    assert all(nf.allowed(cfg, noun, info) for noun, info in COMMANDS.items())
    assert nf.background_services_allowed(cfg)


def test_a_role_shows_its_functions_and_the_commands_every_node_needs(cfg_path):
    cfg = nf.apply(role="collector", functions=["acquire", "transmit"])
    visible = {n for n, i in COMMANDS.items() if nf.allowed(cfg, n, i)}
    assert visible == {"daq", "site", "whoami", "status"}
    assert not nf.background_services_allowed(cfg)
    # Written to disk, and read back the same.
    again = nf.load()
    assert again.role == "collector" and again.functions == ("acquire", "transmit")


def test_a_hidden_command_is_turned_on_and_off_without_a_reinstall(cfg_path):
    nf.apply(role="collector", functions=["acquire", "transmit"])
    cfg = nf.enable("chat", known=COMMANDS)
    assert nf.allowed(cfg, "chat", COMMANDS["chat"])
    cfg = nf.enable("data_platform", known=COMMANDS)
    assert nf.allowed(cfg, "data", COMMANDS["data"])
    cfg = nf.disable("chat")
    assert not nf.allowed(cfg, "chat", COMMANDS["chat"])
    assert nf.allowed(cfg, "data", COMMANDS["data"])
    # The role is unchanged by toggling features.
    assert cfg.functions == ("acquire", "transmit")


def test_background_agents_are_a_feature_like_any_other(cfg_path):
    nf.apply(role="collector", functions=["acquire"])
    cfg = nf.enable(nf.AGENTS_FEATURE, known=COMMANDS)
    assert nf.background_services_allowed(cfg)


def test_a_name_nobody_knows_is_refused_not_silently_saved(cfg_path):
    nf.apply(role="collector", functions=["acquire"])
    with pytest.raises(nf.UnknownFeature):
        nf.enable("frobnicate", known=COMMANDS)
    assert nf.load().features == ()


def test_a_function_outside_the_vocabulary_is_refused(cfg_path):
    with pytest.raises(nf.UnknownFeature):
        nf.apply(role="x", functions=["acquire", "teleport"])


def test_a_role_function_cannot_be_disabled_as_a_feature(cfg_path):
    nf.apply(role="collector", functions=["acquire", "transmit"])
    with pytest.raises(nf.RoleFunction):
        nf.disable("transmit")


def test_clearing_the_role_shows_everything_again_and_keeps_settings(cfg_path):
    nf.apply(role="collector", functions=["acquire"], settings={"daq_config": "/srv/site.toml"})
    cfg = nf.clear()
    assert not cfg.confined
    assert cfg.settings.get("daq_config") == "/srv/site.toml"
    assert all(nf.allowed(cfg, n, i) for n, i in COMMANDS.items())


def test_an_unreadable_config_does_not_lock_anyone_out(cfg_path):
    cfg_path.write_text("this is [not toml", encoding="utf-8")
    cfg = nf.load()
    assert not cfg.confined
    assert cfg.problem  # and it says why


def test_the_feature_list_groups_every_command_by_its_function(cfg_path):
    cfg = nf.apply(role="collector", functions=["acquire", "transmit"])
    rows = {r.name: r for r in nf.feature_rows(cfg, COMMANDS)}
    assert rows["acquire"].enabled and rows["acquire"].source == "role"
    assert not rows["assistant"].enabled and rows["assistant"].commands == ("chat",)
    assert rows["bench"].kind == "command" and not rows["bench"].enabled
    assert rows[nf.AGENTS_FEATURE].kind == "service"
