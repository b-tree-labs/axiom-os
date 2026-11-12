# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the connector config store."""

from __future__ import annotations

from pathlib import Path


def _cfg(name="box-reports", folder_id="100", **overrides):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import ConnectorConfig

    return ConnectorConfig(
        name=name,
        kind="box",
        bronze_root="/tmp/bronze",
        rag_dsn_env="DP1_RAG_DSN",
        provenance_rules_file=None,
        default_disposition="quarantine",
        default_tier="rag-community",
        params={"folder_id": folder_id},
        **overrides,
    )


def test_save_then_load_roundtrip(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        load_connector,
        save_connector,
    )

    cfg = _cfg()
    save_connector(cfg, state_dir=tmp_path)
    loaded = load_connector("box-reports", state_dir=tmp_path)
    assert loaded == cfg


def test_load_missing_raises(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import load_connector

    try:
        load_connector("nope", state_dir=tmp_path)
    except FileNotFoundError:
        return
    raise AssertionError("must raise FileNotFoundError")


def test_list_connectors_returns_all(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        list_connectors,
        save_connector,
    )

    save_connector(_cfg(name="a"), state_dir=tmp_path)
    save_connector(_cfg(name="b"), state_dir=tmp_path)
    rows = list_connectors(state_dir=tmp_path)
    names = sorted(r.name for r in rows)
    assert names == ["a", "b"]


def test_delete_connector_removes_file(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        delete_connector,
        load_connector,
        save_connector,
    )

    save_connector(_cfg(), state_dir=tmp_path)
    assert delete_connector("box-reports", state_dir=tmp_path) is True
    try:
        load_connector("box-reports", state_dir=tmp_path)
    except FileNotFoundError:
        return
    raise AssertionError("connector should have been removed")
