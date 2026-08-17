# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``register-connector`` skill — persist an :class:`IngestSource` config.

Deterministic wiring (no LLM judgment required). Writes a TOML under
PLINTH's connectors dir; subsequent ``run-ingest`` calls load it.

The skill is idempotent against the config shape: re-registering the
same name with the same fields is a no-op (returns ``changed=False``).
A re-register with different fields raises unless ``force=True`` — a
silent overwrite would mask a typo against an existing connector.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..connectors import (
    ConnectorConfig,
    delete_connector,
    list_connectors,
    load_connector,
    save_connector,
)


@dataclass(frozen=True)
class RegisterResult:
    name: str
    changed: bool
    path: Path
    previous: ConnectorConfig | None = None


def register_connector(
    config: ConnectorConfig,
    *,
    force: bool = False,
    state_dir: Path | None = None,
) -> RegisterResult:
    # Kind validation is the SourceKindProvider's job — the persistence
    # helper just persists. The `data.register` skill validates against
    # default_source_kind_registry() BEFORE calling here, so an unknown
    # kind never reaches this helper in normal flow.

    try:
        prev = load_connector(config.name, state_dir=state_dir)
    except FileNotFoundError:
        prev = None

    if prev is not None and prev == config:
        return RegisterResult(name=config.name, changed=False,
                              path=_path_for(config, state_dir), previous=prev)

    if prev is not None and not force:
        raise ValueError(
            f"connector {config.name!r} already exists with different fields; "
            "pass --force to overwrite"
        )

    path = save_connector(config, state_dir=state_dir)
    return RegisterResult(name=config.name, changed=True, path=path, previous=prev)


def _path_for(config: ConnectorConfig, state_dir: Path | None) -> Path:
    from ..connectors import connectors_dir

    return connectors_dir(state_dir=state_dir) / f"{config.name}.toml"


def unregister_connector(name: str, *, state_dir: Path | None = None) -> bool:
    return delete_connector(name, state_dir=state_dir)


__all__ = ["RegisterResult", "list_connectors", "register_connector", "unregister_connector"]
