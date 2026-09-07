# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""P2 wiring: the two tabular kinds register with shape=tabular, and
ConnectorConfig.credential_ref round-trips through save/load."""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    ConnectorConfig,
    load_connector,
    save_connector,
)
from axiom.extensions.builtins.data_platform.sources import (
    default_source_kind_registry,
    source_shape,
)
from axiom.extensions.builtins.data_platform.sources.contracts import SourceKindProvider


def test_tabular_kinds_are_registered_with_tabular_shape():
    reg = default_source_kind_registry()
    for kind in ("http-tabular", "sql-tabular"):
        provider = reg.get(kind)
        assert isinstance(provider, SourceKindProvider)
        assert source_shape(provider) == "tabular"


def test_box_is_still_document_shape():
    # regression: adding tabular kinds must not perturb the document kind
    assert source_shape(default_source_kind_registry().get("box")) == "document"


def test_credential_ref_round_trips(tmp_path: Path):
    cfg = ConnectorConfig(
        name="preds", kind="sql-tabular", bronze_root=str(tmp_path / "b"),
        credential_ref="env://SHADOW_DB_DSN",
        params={"query": "SELECT 1", "schema_ref": "s.v1"},
    )
    save_connector(cfg, state_dir=tmp_path)
    loaded = load_connector("preds", state_dir=tmp_path)
    assert loaded.credential_ref == "env://SHADOW_DB_DSN"
    assert loaded == cfg


def test_connector_without_credential_ref_defaults_none(tmp_path: Path):
    # back-compat: an older-shaped connector (no credential_ref) still loads
    cfg = ConnectorConfig(name="docs", kind="box", bronze_root=str(tmp_path / "b"),
                          params={"folder_id": "123"})
    save_connector(cfg, state_dir=tmp_path)
    assert load_connector("docs", state_dir=tmp_path).credential_ref is None
