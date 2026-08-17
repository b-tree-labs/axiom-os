# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Connector registry — generic across ingest-source kinds.

A *connector* is a named, persisted source configuration. The
platform owns the kind-agnostic fields (``name``, ``kind``,
``bronze_root``, ``default_disposition``, ``default_tier``,
``provenance_rules_file``, ``rag_dsn_env``); each
:class:`SourceKindProvider` owns its kind-specific fields and stores
them under :attr:`ConnectorConfig.params`.

TOML on-disk shape (Box example)::

    [connector]
    name = "reports-corpus"
    kind = "box"
    bronze_root = "/var/lib/axiom/bronze"
    rag_dsn_env = "DP1_RAG_DSN"
    provenance_rules_file = "/etc/axiom/rules/rules.toml"
    default_disposition = "allow"
    default_tier = "rag-community"

    [connector.params]
    folder_id = "12345"
    session_state_b64 = "<...>"

Layout (default ``$AXI_STATE/plinth/connectors/``)::

    <connector-name>.toml
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from axiom.infra.paths import get_user_state_dir


@dataclass(frozen=True)
class ConnectorConfig:
    """Persisted shape for one ingest-source instance.

    Kind-specific binding (Box's ``folder_id``, GDrive's ``drive_id``)
    lives under :attr:`params`. The platform doesn't read these — only
    the kind's :class:`SourceKindProvider` does.
    """

    name: str
    kind: str
    bronze_root: str
    rag_dsn_env: str = "DP1_RAG_DSN"
    provenance_rules_file: str | None = None
    default_disposition: str = "quarantine"
    default_tier: str | None = "rag-community"
    credential_ref: str | None = None
    """Optional SecretRef URL (ADR-001 D5) resolving to this connector's
    credential — e.g. a DSN for ``sql-tabular`` (``env://SHADOW_DB_DSN`` or
    ``openbao://host/db/dsn``). Platform-generic so any kind can use it; the
    kind's provider resolves it through the secrets extension at
    construct/preflight time, so the secret never lands in the TOML."""
    promotion_map_file: str | None = None
    """Optional path to a declared bronze→gold promotion map (ADR-001 D4:
    column map + optional EAV pivot + source precedence + SCD-2). Validated at
    register time, so a bad map is a register error, not a runtime crash."""
    params: dict[str, str] = field(default_factory=dict)
    """Kind-specific values. The :class:`SourceKindProvider` for
    ``kind`` is the only thing that reads these — the platform never
    speaks any one source's vocabulary."""

    def with_params(self, **overrides: str) -> ConnectorConfig:
        """Return a copy with ``params`` merged with ``overrides``."""
        return replace(self, params={**self.params, **overrides})


def connectors_dir(*, state_dir: Path | None = None) -> Path:
    """Where connector TOMLs live."""
    base = state_dir or get_user_state_dir()
    return base / "plinth" / "connectors"


def save_connector(config: ConnectorConfig, *, state_dir: Path | None = None) -> Path:
    """Persist a connector to TOML; returns the written path."""
    d = connectors_dir(state_dir=state_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{config.name}.toml"

    lines = ["[connector]"]
    for k, v in (
        ("name", config.name),
        ("kind", config.kind),
        ("bronze_root", config.bronze_root),
        ("rag_dsn_env", config.rag_dsn_env),
        ("provenance_rules_file", config.provenance_rules_file),
        ("default_disposition", config.default_disposition),
        ("default_tier", config.default_tier),
        ("credential_ref", config.credential_ref),
        ("promotion_map_file", config.promotion_map_file),
    ):
        if v is None:
            continue
        lines.append(f"{k} = {_toml_str(str(v))}")

    if config.params:
        lines.append("")
        lines.append("[connector.params]")
        for k, v in config.params.items():
            lines.append(f"{k} = {_toml_str(str(v))}")

    path.write_text("\n".join(lines) + "\n")
    return path


def load_connector(name: str, *, state_dir: Path | None = None) -> ConnectorConfig:
    path = connectors_dir(state_dir=state_dir) / f"{name}.toml"
    if not path.exists():
        raise FileNotFoundError(f"no connector named {name!r} at {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    connector = data.get("connector") or {}
    params = connector.pop("params", {}) if isinstance(connector.get("params"), dict) else {}
    return ConnectorConfig(**connector, params={str(k): str(v) for k, v in params.items()})


def list_connectors(*, state_dir: Path | None = None) -> list[ConnectorConfig]:
    d = connectors_dir(state_dir=state_dir)
    if not d.exists():
        return []
    out: list[ConnectorConfig] = []
    for p in sorted(d.glob("*.toml")):
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            connector = data.get("connector") or {}
            params = connector.pop("params", {}) if isinstance(connector.get("params"), dict) else {}
            out.append(
                ConnectorConfig(
                    **connector,
                    params={str(k): str(v) for k, v in params.items()},
                )
            )
        except (tomllib.TOMLDecodeError, TypeError):
            # A broken file shouldn't take down `list`; surface what loads.
            continue
    return out


def delete_connector(name: str, *, state_dir: Path | None = None) -> bool:
    path = connectors_dir(state_dir=state_dir) / f"{name}.toml"
    if path.exists():
        path.unlink()
        return True
    return False


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


__all__ = [
    "ConnectorConfig",
    "connectors_dir",
    "delete_connector",
    "list_connectors",
    "load_connector",
    "save_connector",
]
