# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Postgres database-kind provider."""

from __future__ import annotations

# Idiomatic self-registration on first import.
from ..registry import default_database_kind_registry
from .provider import PostgresDatabaseProvider

if not default_database_kind_registry().has("postgres"):
    default_database_kind_registry().register(PostgresDatabaseProvider())

__all__ = ["PostgresDatabaseProvider"]
