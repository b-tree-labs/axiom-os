# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the kind-agnostic source spec resolver.

These exercise ``_iter_sources`` / ``_build_source`` — pure-Python
resolution with no dagster dependency — so they run without the
``[data-platform]`` extra. They cover the connector path for any
registered kind, the Box-only env back-compat shim, and that
construction routes through the :class:`SourceKindRegistry` (not a
Box special-case).
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.dagster_app.defs import (
    SourceSpec,
    _build_source,
    _build_writer,
    _close_source,
    _iter_sources,
    _sources_from_connectors,
)


@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch, tmp_path):
    """Point connector discovery at an empty dir so the env-path tests
    don't pick up connector TOMLs from the developer's real ~/.axi."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))


def _clear(monkeypatch):
    for var in (
        "DP1_BOX_SOURCES",
        "DP1_BOX_FOLDER_ID",
        "DP1_BOX_SOURCE_NAME",
        "DP1_BOX_DEFAULT_TIER",
        "BOX_CCG_CONFIG",
        "BOX_JWT_CONFIG",
        "NEUT_BOX_SESSION_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


# ---- Box env back-compat shim -----------------------------------------


def test_single_folder_env_is_backward_compatible(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DP1_BOX_FOLDER_ID", "363592758132")
    monkeypatch.setenv("DP1_BOX_SOURCE_NAME", "research-corpus")

    specs = _iter_sources()

    assert specs == [
        SourceSpec(
            kind="box",
            name="research-corpus",
            default_tier=None,
            params={"folder_id": "363592758132"},
        )
    ]


def test_single_folder_default_name_and_optional_tier(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DP1_BOX_FOLDER_ID", "5")
    monkeypatch.setenv("DP1_BOX_DEFAULT_TIER", "rag-org")

    specs = _iter_sources()

    assert specs == [
        SourceSpec(kind="box", name="box", default_tier="rag-org", params={"folder_id": "5"})
    ]


def test_sources_json_list_yields_multiple(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(
        "DP1_BOX_SOURCES",
        '[{"name": "research-corpus", "folder_id": "363592758132", '
        '"default_tier": "rag-community"}, '
        '{"name": "dept-archive", "folder_id": "228326101313", '
        '"default_tier": "rag-org"}]',
    )

    specs = _iter_sources()

    assert specs == [
        SourceSpec("box", "research-corpus", "rag-community", {"folder_id": "363592758132"}),
        SourceSpec("box", "dept-archive", "rag-org", {"folder_id": "228326101313"}),
    ]


def test_sources_json_takes_precedence_over_single_env(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DP1_BOX_FOLDER_ID", "999")
    monkeypatch.setenv(
        "DP1_BOX_SOURCES",
        '[{"name": "dept-archive", "folder_id": "228326101313"}]',
    )

    specs = _iter_sources()

    assert [s.params["folder_id"] for s in specs] == ["228326101313"]


def test_empty_env_returns_no_specs(monkeypatch):
    _clear(monkeypatch)
    assert _iter_sources() == []


def test_duplicate_names_rejected(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(
        "DP1_BOX_SOURCES",
        '[{"name": "dup", "folder_id": "1"}, {"name": "dup", "folder_id": "2"}]',
    )
    with pytest.raises(ValueError, match="duplicate"):
        _iter_sources()


def test_missing_folder_id_rejected(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DP1_BOX_SOURCES", '[{"name": "x"}]')
    with pytest.raises(ValueError, match="folder_id"):
        _iter_sources()


def test_malformed_json_rejected(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DP1_BOX_SOURCES", "{not json")
    with pytest.raises(ValueError, match="DP1_BOX_SOURCES"):
        _iter_sources()


# ---- Box server-auth env → SecretRef bridging --------------------------
#
# Regression (RATIONALIZE / Box OAuth fix, 2026-06-30): the Dagster path
# once only understood BOX_JWT_CONFIG (JWT keypair), so an OAuth/CCG blob
# in BOX_CCG_CONFIG was ignored and the pod fell back to the dead dev
# token -> 401. The env shim must reference the blob env as
# `jwt_secret_ref=env://<VAR>` so the Box provider resolves + dispatches
# it by shape — the same path connector TOMLs use.


def test_env_ccg_config_becomes_secret_ref_on_every_spec(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BOX_CCG_CONFIG", '{"client_id": "c"}')
    monkeypatch.setenv(
        "DP1_BOX_SOURCES",
        '[{"name": "a", "folder_id": "1"}, {"name": "b", "folder_id": "2"}]',
    )

    specs = _iter_sources()

    assert [s.params["jwt_secret_ref"] for s in specs] == [
        "env://BOX_CCG_CONFIG",
        "env://BOX_CCG_CONFIG",
    ]


def test_env_jwt_config_becomes_secret_ref_when_no_ccg(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BOX_JWT_CONFIG", '{"boxAppSettings": {}}')
    monkeypatch.setenv("DP1_BOX_FOLDER_ID", "5")

    (spec,) = _iter_sources()

    assert spec.params["jwt_secret_ref"] == "env://BOX_JWT_CONFIG"


def test_env_ccg_config_wins_over_jwt_config(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BOX_CCG_CONFIG", '{"client_id": "c"}')
    monkeypatch.setenv("BOX_JWT_CONFIG", '{"boxAppSettings": {}}')
    monkeypatch.setenv("DP1_BOX_FOLDER_ID", "5")

    (spec,) = _iter_sources()

    assert spec.params["jwt_secret_ref"] == "env://BOX_CCG_CONFIG"


def test_no_auth_env_leaves_params_ref_free(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DP1_BOX_FOLDER_ID", "5")

    (spec,) = _iter_sources()

    assert "jwt_secret_ref" not in spec.params


def test_session_dir_env_flows_into_params(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("NEUT_BOX_SESSION_DIR", str(tmp_path / "sess"))
    monkeypatch.setenv("DP1_BOX_FOLDER_ID", "5")

    (spec,) = _iter_sources()

    assert spec.params["session_dir"] == str(tmp_path / "sess")


def test_env_ccg_blob_dispatches_to_ccg_auth(monkeypatch):
    """End-to-end: a CCG-shaped blob in BOX_CCG_CONFIG reaches the
    constructed source's API client as BoxCcgAuth (not the dev token)."""
    import json

    from axiom.extensions.builtins.data_platform.sources.box.ccg_auth import (
        BoxCcgAuth,
    )

    _clear(monkeypatch)
    monkeypatch.setenv(
        "BOX_CCG_CONFIG",
        json.dumps({"client_id": "c", "client_secret": "s", "enterprise_id": "134853"}),
    )
    monkeypatch.setenv("DP1_BOX_FOLDER_ID", "5")

    (spec,) = _iter_sources()
    source = _build_source(spec)

    assert isinstance(source._api._jwt_auth, BoxCcgAuth)


def test_env_oauth_blob_dispatches_to_oauth_auth(monkeypatch, tmp_path):
    """End-to-end: an OAuth refresh-token blob dispatches to BoxOAuthAuth."""
    import json

    from axiom.extensions.builtins.data_platform.sources.box.oauth_auth import (
        BoxOAuthAuth,
    )

    _clear(monkeypatch)
    monkeypatch.setenv(
        "BOX_CCG_CONFIG",
        json.dumps({"client_id": "c", "client_secret": "s",
                    "refresh_token": "r", "token_store": str(tmp_path / "x.json")}),
    )
    monkeypatch.setenv("DP1_BOX_FOLDER_ID", "5")

    (spec,) = _iter_sources()
    source = _build_source(spec)

    assert isinstance(source._api._jwt_auth, BoxOAuthAuth)


# ---- connector path (kind-agnostic) -----------------------------------


def test_connector_tomls_are_primary_source(monkeypatch, tmp_path):
    """Box connector TOMLs win over env (the documented design)."""
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    _clear(monkeypatch)
    # env present but must be ignored when connectors exist
    monkeypatch.setenv("DP1_BOX_FOLDER_ID", "999")

    state = tmp_path / "state"
    save_connector(
        ConnectorConfig(
            name="dept-archive",
            kind="box",
            bronze_root="/var/lib/axiom/bronze",
            default_tier="rag-org",
            params={"folder_id": "228326101313"},
        ),
        state_dir=state,
    )
    monkeypatch.setenv("AXI_STATE_DIR", str(state))

    specs = _iter_sources()

    assert specs == [
        SourceSpec("box", "dept-archive", "rag-org", {"folder_id": "228326101313"})
    ]


def test_unknown_kind_connector_is_skipped(monkeypatch, tmp_path):
    """A connector whose kind isn't registered is dropped, not fatal."""
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )
    from axiom.extensions.builtins.data_platform.sources import (
        default_source_kind_registry,
    )

    _clear(monkeypatch)
    state = tmp_path / "state"
    save_connector(
        ConnectorConfig(
            name="dept-archive",
            kind="box",
            bronze_root="/b",
            default_tier="rag-org",
            params={"folder_id": "228326101313"},
        ),
        state_dir=state,
    )
    save_connector(
        ConnectorConfig(name="some-s3", kind="s3", bronze_root="/b", params={"bucket": "x"}),
        state_dir=state,
    )
    monkeypatch.setenv("AXI_STATE_DIR", str(state))

    # The default registry has box but not s3 → s3 connector skipped.
    assert not default_source_kind_registry().has("s3")
    specs = _iter_sources()
    assert [s.name for s in specs] == ["dept-archive"]


# ---- registry-factory construction path -------------------------------


class _FakeSource:
    """Minimal IngestSource for a fake kind."""

    def __init__(self, name, params):
        self.name = name
        self.params = params
        self.closed = False

    def list_changed(self, since=None):
        return []

    def fetch(self, item):  # pragma: no cover - not driven here
        raise NotImplementedError

    def close(self):
        self.closed = True


class _FakeProvider:
    """A non-box :class:`SourceKindProvider` for a made-up kind."""

    kind = "fake"
    description = "fake kind for tests"

    def add_register_args(self, subparser):  # pragma: no cover
        ...

    def params_from_args(self, args):  # pragma: no cover
        return {}

    def validate(self, config):
        return []

    def construct(self, config):
        return _FakeSource(config.name, dict(config.params))

    def url_for(self, config, ref_id):
        # URL-less kind — exempt by declaration per ADR-075.
        return None

    def preflight(self, config):  # pragma: no cover - not driven here
        from axiom.extensions.builtins.data_platform.sources.contracts import (
            PreflightResult,
        )

        return PreflightResult(connector=config.name, kind=self.kind)


def test_connector_flows_through_registry_for_non_box_kind(monkeypatch, tmp_path):
    """A NON-box registered kind is resolved + constructed via the
    registry factory — proving defs.py is not Box-special-cased."""
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )
    from axiom.extensions.builtins.data_platform.sources import SourceKindRegistry

    _clear(monkeypatch)
    state = tmp_path / "state"
    save_connector(
        ConnectorConfig(
            name="my-fake",
            kind="fake",
            bronze_root="/b",
            default_tier="rag-org",
            params={"endpoint": "x"},
        ),
        state_dir=state,
    )
    monkeypatch.setenv("AXI_STATE_DIR", str(state))

    registry = SourceKindRegistry()
    registry.register(_FakeProvider())

    specs = _sources_from_connectors(registry=registry)
    assert specs == [
        SourceSpec("fake", "my-fake", "rag-org", {"endpoint": "x"})
    ]

    source = _build_source(specs[0], registry=registry)
    assert isinstance(source, _FakeSource)
    assert source.name == "my-fake"
    assert source.params == {"endpoint": "x"}


# ---- source lifecycle ---------------------------------------------------


def test_close_source_closes_owned_client():
    source = _FakeSource("x", {})
    _close_source(source)
    assert source.closed is True


def test_close_source_tolerates_closeless_source():
    class _NoClose:
        pass

    _close_source(_NoClose())  # must not raise


# ---- writer resolution ----------------------------------------------------
#
# `_build_writer` must prefer the connector registry (single source of
# bronze root + rules + tier — no split brain with the CLI/HTTP/MCP
# paths) and only fall back to the legacy env writer, carrying the
# spec's default_tier, when the connector is not registered.


def test_build_writer_prefers_registered_connector(monkeypatch, tmp_path):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    _clear(monkeypatch)
    state = tmp_path / "state"
    bronze_root = tmp_path / "connector-bronze"
    save_connector(
        ConnectorConfig(
            name="my-src",
            kind="box",
            bronze_root=str(bronze_root),
            default_tier="rag-org",
            params={"folder_id": "1"},
        ),
        state_dir=state,
    )
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    # env root present but must lose to the connector's root
    monkeypatch.setenv("DP1_BRONZE_ROOT", str(tmp_path / "env-bronze"))

    writer = _build_writer(SourceSpec(kind="box", name="my-src", params={"folder_id": "1"}))

    assert writer._sink.root == bronze_root
    assert writer._default_tier == "rag-org"


def test_build_writer_falls_back_to_env_writer_with_spec_tier(monkeypatch, tmp_path):
    from axiom.rag.ingest_router import Disposition

    _clear(monkeypatch)
    monkeypatch.delenv("DP1_PROVENANCE_RULES_FILE", raising=False)
    env_root = tmp_path / "env-bronze"
    monkeypatch.setenv("DP1_BRONZE_ROOT", str(env_root))

    writer = _build_writer(
        SourceSpec(kind="box", name="unregistered", default_tier="rag-internal",
                   params={"folder_id": "1"})
    )

    assert writer._sink.root == env_root
    assert writer._default_tier == "rag-internal"
    assert writer._default_disposition == Disposition.QUARANTINE


# ---- asset naming ------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("research-corpus", "corpus__research_corpus"),
        ("dept-archive", "corpus__dept_archive"),
        ("UT NE", "corpus__ut_ne"),
    ],
)
def test_asset_name_sanitization(name, expected):
    assert SourceSpec(kind="box", name=name, params={"folder_id": "1"}).asset_name == expected
