# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared object storage abstractions.

Provides a StorageProvider ABC and concrete implementations (local filesystem,
S3-compatible) that any extension can use for artifact storage.
"""

from axiom.infra.storage.base import (
    StorageEntry,
    StorageProvider,
    UploadResult,
)
from axiom.infra.storage.local import LocalStorageProvider

__all__ = [
    "LocalStorageProvider",
    "StorageEntry",
    "StorageProvider",
    "UploadResult",
]

# S3StorageProvider is available via axiom.infra.storage.s3 when boto3 is installed.
# Not imported here to avoid hard dependency on boto3 for users who don't need S3.
