# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ported from SoilMetrix ``tests/unit/test_feature_flag_service.py`` (11 tests),
then the Axiom additions: node scope, on/off/soon, the full precedence chain.

SoilMetrix stubbed a SQLAlchemy query; here the service takes an override store,
and the stand-in below holds rows in a dict. The flag names are the conftest's
domain-neutral stand-ins for the SoilMetrix registry.
"""

from __future__ import annotations

import uuid

import pytest

from axiom.extensions.builtins.flags.service import FlagError, FlagService


class _StubStore:
    """``{(scope, scope_id): {key: value}}``; records writes like the real store."""

    def __init__(self, rows=None):
        self.rows = {k: dict(v) for k, v in (rows or {}).items()}
        self.writes = []

    def overrides_for(self, scope, scope_id):
        return dict(self.rows.get((scope, str(scope_id)), {})) if scope_id else {}

    def put(self, scope, scope_id, key, value, *, by):
        self.rows.setdefault((scope, str(scope_id)), {})[key] = value
        self.writes.append((scope, scope_id, key, value, by))
        return {"scope": scope, "scope_id": scope_id, "key": key, "value": value, "updated_by": by}

    def delete(self, scope, scope_id, key):
        return self.rows.get((scope, str(scope_id)), {}).pop(key, None) is not None


# ------------------------------------------------------------ ported (SoilMetrix)


def test_feature_flags_default_to_registry_value():
    service = FlagService(_StubStore())
    assert service.resolve(user="u1").flags["history_timeline"] == "off"


def test_feature_flags_apply_account_then_user_override():
    account_id = str(uuid.uuid4())
    store = _StubStore({
        ("account", account_id): {"history_timeline": "on"},
        ("user", "u1"): {"history_timeline": "off"},
    })
    service = FlagService(store)
    assert service.resolve(account=account_id, user="u1").flags["history_timeline"] == "off"


def test_feature_flags_respect_environment_override(monkeypatch):
    monkeypatch.setenv("AXIOM_FLAG_HISTORY_TIMELINE", "true")
    service = FlagService(_StubStore())
    assert service.is_enabled("history_timeline", user="u1") is True


def test_build_plans_protected_in_production(monkeypatch):
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "production")
    service = FlagService(_StubStore({("user", "u1"): {"beta_planner": "on"}}))
    assert service.resolve(user="u1").flags["beta_planner"] == "off"


def test_get_flag_metadata_marks_protected_flags(monkeypatch):
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "production")
    metadata = FlagService(_StubStore()).get_flag_metadata()
    assert metadata["beta_planner"]["protected"] is True


def test_toggle_flag_for_user_updates_override():
    """SoilMetrix toggled; Axiom sets an explicit value (a toggle is not idempotent)."""
    store = _StubStore()
    service = FlagService(store)
    row = service.set_override("user", "u1", "history_timeline", True, by="@u1")
    assert row["value"] == "on"
    assert store.rows[("user", "u1")]["history_timeline"] == "on"
    assert store.writes == [("user", "u1", "history_timeline", "on", "@u1")]
    assert service.resolve(user="u1").flags["history_timeline"] == "on"


def test_toggle_unknown_flag_returns_none():
    with pytest.raises(FlagError) as exc:
        FlagService(_StubStore()).set_override("user", "u1", "does_not_exist", "on", by="@u1")
    assert exc.value.code == "unknown"


def test_resolve_flag_for_account_env_default_dev(monkeypatch):
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "development")
    assert FlagService(_StubStore()).resolve_flag("gold_first_reads") == "on"


def test_resolve_flag_for_account_dark_in_production(monkeypatch):
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "production")
    service = FlagService(_StubStore())
    assert service.resolve_flag("gold_first_reads", account=str(uuid.uuid4())) == "off"


def test_resolve_flag_for_account_account_override(monkeypatch):
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "staging")
    account_id = str(uuid.uuid4())
    service = FlagService(_StubStore({("account", account_id): {"gold_first_reads": "on"}}))
    assert service.resolve_flag("gold_first_reads", account=account_id) == "on"


def test_resolve_flag_for_account_unknown_flag_false():
    assert FlagService(_StubStore()).resolve_flag("nonexistent_flag") == "off"


# ------------------------------------------------------------ Axiom additions


def _layered(monkeypatch, layers):
    """A store with an override at each named layer, values distinct per layer."""
    rows = {}
    if "node" in layers:
        rows[("node", "node-a")] = {"history_timeline": layers["node"]}
    if "account" in layers:
        rows[("account", "acct")] = {"history_timeline": layers["account"]}
    if "user" in layers:
        rows[("user", "u1")] = {"history_timeline": layers["user"]}
    if "env" in layers:
        monkeypatch.setenv("AXIOM_FLAG_HISTORY_TIMELINE", layers["env"])
    res = FlagService(_StubStore(rows)).resolve(node="node-a", account="acct", user="u1")
    return res.flags["history_timeline"], res.sources["history_timeline"]


@pytest.mark.parametrize(
    "layers, expected",
    [
        ({}, ("off", "default")),
        ({"env": "on"}, ("on", "env")),
        ({"env": "on", "node": "soon"}, ("soon", "node")),
        ({"env": "on", "node": "soon", "account": "off"}, ("off", "account")),
        ({"env": "off", "node": "soon", "account": "off", "user": "on"}, ("on", "user")),
        ({"node": "on", "user": "soon"}, ("soon", "user")),
        ({"account": "on"}, ("on", "account")),
    ],
)
def test_precedence_default_env_node_account_user(monkeypatch, layers, expected):
    assert _layered(monkeypatch, layers) == expected


def test_a_protected_flag_ignores_node_account_and_user_but_not_env(monkeypatch):
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "production")
    store = _StubStore({
        ("node", "node-a"): {"beta_planner": "on"},
        ("account", "acct"): {"beta_planner": "on"},
        ("user", "u1"): {"beta_planner": "on"},
    })
    service = FlagService(store)
    res = service.resolve(node="node-a", account="acct", user="u1")
    assert (res.flags["beta_planner"], res.sources["beta_planner"]) == ("off", "default")
    assert service.resolve_flag("beta_planner", node="node-a", account="acct", user="u1") == "off"
    monkeypatch.setenv("AXIOM_FLAG_BETA_PLANNER", "on")
    assert service.resolve(node="node-a", user="u1").flags["beta_planner"] == "on"


def test_a_protected_flag_is_not_protected_elsewhere(monkeypatch):
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "staging")
    service = FlagService(_StubStore({("user", "u1"): {"beta_planner": "on"}}))
    assert service.resolve(user="u1").flags["beta_planner"] == "on"


def test_setting_a_protected_flag_is_refused(monkeypatch):
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "production")
    store = _StubStore()
    with pytest.raises(FlagError) as exc:
        FlagService(store).set_override("node", "node-a", "beta_planner", "on", by="@op")
    assert exc.value.code == "protected" and store.writes == []


def test_soon_is_a_third_state_and_a_registry_default(monkeypatch):
    service = FlagService(_StubStore())
    assert service.resolve().flags["library"] == "soon"
    monkeypatch.setenv("AXIOM_FLAG_LIBRARY", "soon")
    assert service.is_enabled("library") is False  # soon is not on


@pytest.mark.parametrize("raw, want", [(True, "on"), (False, "off"), ("yes", "on"),
                                       ("0", "off"), ("SOON", "soon"), ("enabled", "on")])
def test_values_normalize_to_on_off_soon(raw, want):
    store = _StubStore()
    FlagService(store).set_override("user", "u1", "history_timeline", raw, by="@u1")
    assert store.rows[("user", "u1")]["history_timeline"] == want


def test_a_value_that_is_not_on_off_or_soon_is_refused():
    with pytest.raises(FlagError) as exc:
        FlagService(_StubStore()).set_override("user", "u1", "history_timeline", "maybe", by="@u1")
    assert exc.value.code == "invalid"


def test_an_unparseable_env_value_is_ignored(monkeypatch):
    monkeypatch.setenv("AXIOM_FLAG_HISTORY_TIMELINE", "perhaps")
    assert FlagService(_StubStore()).resolve().sources["history_timeline"] == "default"


def test_clearing_an_override_falls_back_to_the_layer_below():
    store = _StubStore({("node", "node-a"): {"history_timeline": "on"},
                        ("user", "u1"): {"history_timeline": "soon"}})
    service = FlagService(store)
    assert service.set_override("user", "u1", "history_timeline", None, by="@u1") is None
    res = service.resolve(node="node-a", user="u1")
    assert (res.flags["history_timeline"], res.sources["history_timeline"]) == ("on", "node")


def test_default_by_env_falls_back_to_the_flat_default_for_an_unlisted_env(monkeypatch):
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "canary")
    assert FlagService(_StubStore()).resolve_flag("gold_first_reads") == "off"


def test_the_first_deploy_values_are_expressible(monkeypatch):
    """A domain product's first deploy (control/log off, library soon, next_cards off) is a
    node-scope set over whatever the declaring extension defaults to."""
    from axiom.extensions.builtins.flags import registry as reg
    from axiom.extensions.builtins.flags.registry import FlagDef

    reg.set_registry([FlagDef(key=k, default="on") for k in ("control", "log", "library", "next_cards")])
    store = _StubStore()
    service = FlagService(store)
    for key, value in (("control", "off"), ("log", "off"), ("library", "soon"), ("next_cards", "off")):
        service.set_override("node", "node-a", key, value, by="@cli:local")
    assert service.resolve(node="node-a", user="anyone").flags == {
        "library": "soon", "log": "off", "next_cards": "off", "control": "off",
    }
