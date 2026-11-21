# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared object storage abstractions.

Provides a StorageProvider ABC and concrete implementations (local filesystem,
S3-compatible) that any extension can use for artifact storage.

**The provider is configuration.** A caller names what it stores (a *purpose*, such as
``attest-blobs``) and :func:`configured` returns the provider this node's configuration names
for it: ``AXIOM_STORAGE_<PURPOSE>`` holds a JSON object whose ``provider`` key is a name
registered here (``local`` by default, ``s3`` for any S3-compatible store) and whose other keys
are that provider's own configuration. With nothing configured, a purpose is stored on the
local filesystem under the directory its caller names. So no caller names a provider or vendor.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from axiom.infra.storage.base import (
    StorageEntry,
    StorageProvider,
    UploadResult,
)
from axiom.infra.storage.local import LocalStorageProvider


def _s3(config: dict[str, Any]) -> StorageProvider:
    from axiom.infra.storage.s3 import S3StorageProvider

    return S3StorageProvider(config)


_PROVIDERS: dict[str, Callable[[dict[str, Any]], StorageProvider]] = {
    "local": LocalStorageProvider,
    "s3": _s3,
}

_PURPOSE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def register_provider(name: str, factory: Callable[[dict[str, Any]], StorageProvider]) -> None:
    """Make a provider nameable in configuration (an extension's own store, a test double)."""
    _PROVIDERS[name] = factory


def provider_from_config(config: Mapping[str, Any]) -> StorageProvider:
    """The provider a configuration names (``provider``, default ``local``), built with it."""
    cfg = dict(config)
    name = str(cfg.pop("provider", "local"))
    try:
        factory = _PROVIDERS[name]
    except KeyError:
        raise ValueError(f"no storage provider {name!r}; known: {sorted(_PROVIDERS)}") from None
    return factory(cfg)


def env_name(purpose: str) -> str:
    return "AXIOM_STORAGE_" + purpose.upper().replace("-", "_")


def configured(purpose: str, *, default_dir: Path) -> StorageProvider:
    """The provider this node's configuration names for ``purpose``; with none, the local
    filesystem under ``default_dir``. A configuration that is not a JSON object is refused."""
    if not _PURPOSE.match(purpose):
        raise ValueError(f"storage purpose {purpose!r} is not a lowercase name")
    raw = os.environ.get(env_name(purpose), "").strip()
    if not raw:
        return provider_from_config({"provider": "local", "base_dir": str(default_dir)})
    try:
        config = json.loads(raw)
    except ValueError:
        raise ValueError(f"{env_name(purpose)} is not JSON") from None
    if not isinstance(config, dict):
        raise ValueError(f"{env_name(purpose)} is not a JSON object")
    if config.get("provider", "local") == "local":
        config.setdefault("base_dir", str(default_dir))
    return provider_from_config(config)


__all__ = [
    "LocalStorageProvider",
    "StorageEntry",
    "StorageProvider",
    "UploadResult",
    "configured",
    "env_name",
    "provider_from_config",
    "register_provider",
]

# S3StorageProvider is available via axiom.infra.storage.s3 when boto3 is installed.
# Not imported here to avoid hard dependency on boto3 for users who don't need S3.
