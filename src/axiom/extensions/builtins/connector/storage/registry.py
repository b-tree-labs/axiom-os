# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Storage-connector registry (ADR-110 §Decision-1) — vendor → factory over the
one shared ``ConnectorRegistry``. A new file-store backend (S3, Box, OneDrive) is
one ``register_storage_connector`` call, and nothing above the fabric changes."""

from __future__ import annotations

from typing import Any

from axiom.infra.connector_registry import ConnectorRegistry

_STORES: ConnectorRegistry = ConnectorRegistry("storage connector")


def register_storage_connector(vendor: str, factory: Any, *, replace: bool = False) -> None:
    _STORES.register(vendor, factory, replace=replace)


def available_storage_connectors() -> tuple[str, ...]:
    return _STORES.available()


def get_storage_connector(vendor: str, **config: Any):
    return _STORES.create(vendor, **config)


def _local_store(*, root: str, **_: Any):
    from .local import LocalStorageConnector

    return LocalStorageConnector(root=root)


register_storage_connector("local", _local_store)


__all__ = [
    "available_storage_connectors",
    "get_storage_connector",
    "register_storage_connector",
]
