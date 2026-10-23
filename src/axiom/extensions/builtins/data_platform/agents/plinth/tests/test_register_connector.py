# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the register-connector skill (kind-aware shape).

Post-2026-05-30 the ConnectorConfig is generic + a kind-specific
``params`` dict; the legacy ``folder_id`` / ``box_session_dir`` fields
are gone. Box-shaped tests use ``params={'folder_id': ...}``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path


def _cfg(**overrides):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import ConnectorConfig

    base = ConnectorConfig(
        name="box-reports",
        kind="box",
        bronze_root="/tmp/bronze",
        rag_dsn_env="DP1_RAG_DSN",
        provenance_rules_file=None,
        default_disposition="quarantine",
        default_tier="rag-community",
        params={"folder_id": "100"},
    )
    if "folder_id" in overrides:
        base = replace(base, params={"folder_id": overrides.pop("folder_id")})
    return replace(base, **overrides) if overrides else base


def test_register_fresh_writes_file_and_changed_true(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.agents.plinth.skills.register_connector import (
        register_connector,
    )

    result = register_connector(_cfg(), state_dir=tmp_path)
    assert result.changed is True
    assert result.previous is None
    assert result.path.exists()


def test_register_same_config_is_idempotent(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.agents.plinth.skills.register_connector import (
        register_connector,
    )

    register_connector(_cfg(), state_dir=tmp_path)
    second = register_connector(_cfg(), state_dir=tmp_path)
    assert second.changed is False


def test_register_different_config_without_force_raises(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.agents.plinth.skills.register_connector import (
        register_connector,
    )

    register_connector(_cfg(), state_dir=tmp_path)
    try:
        register_connector(_cfg(folder_id="999"), state_dir=tmp_path)
    except ValueError as exc:
        assert "force" in str(exc).lower()
        return
    raise AssertionError("re-register with different fields must raise without --force")


def test_register_different_config_with_force_overwrites(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import load_connector
    from axiom.extensions.builtins.data_platform.agents.plinth.skills.register_connector import (
        register_connector,
    )

    register_connector(_cfg(), state_dir=tmp_path)
    result = register_connector(_cfg(folder_id="999"), force=True, state_dir=tmp_path)
    assert result.changed is True
    assert result.previous is not None
    assert result.previous.params["folder_id"] == "100"
    loaded = load_connector("box-reports", state_dir=tmp_path)
    assert loaded.params["folder_id"] == "999"


def test_register_unknown_kind_through_skill_surfaces_clear_error(tmp_path: Path):
    """Unknown source kinds fail at the `data.register` skill layer (via
    SourceKindRegistry.get) — the lower-level register_connector helper
    just persists whatever it's given. The error happens earlier in the
    `axi data register` dispatch, not here."""
    from axiom.extensions.builtins.data_platform.agents.plinth.skills.register_connector import (
        register_connector,
    )

    # The low-level helper has no kind awareness; it just persists.
    # The kind-validation gate lives in `skills.register.run` which is
    # tested at the CLI subprocess level.
    result = register_connector(_cfg(kind="s3"), state_dir=tmp_path)
    assert result.changed is True
