# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for the ``gate`` account-admin + API-key skills.

Where the accounts / API-keys files live, and how to mint a strong temporary
password when the admin does not supply one.
"""

from __future__ import annotations

import os
import secrets
import string
from pathlib import Path
from typing import Any

from axiom.webauth import validate_password

ACCOUNTS_ENV = "AXIOM_GATE_USERS_FILE"
KEYS_ENV = "AXIOM_GATE_API_KEYS_FILE"

_ALPHABET = string.ascii_letters + string.digits


def resolve_accounts_path(params: dict[str, Any]) -> Path | None:
    """The accounts file from ``--accounts-file`` or ``$AXIOM_GATE_USERS_FILE``.

    ``None`` when neither is set — the skill turns that into a clear error.
    """
    raw = params.get("accounts_file") or os.environ.get(ACCOUNTS_ENV)
    return Path(raw) if raw else None


def resolve_keys_path(params: dict[str, Any]) -> Path | None:
    """The API-keys file from ``--keys-file`` or ``$AXIOM_GATE_API_KEYS_FILE``.

    ``None`` when neither is set — the skill turns that into a clear error.
    """
    raw = params.get("keys_file") or os.environ.get(KEYS_ENV)
    return Path(raw) if raw else None


def generate_password(length: int = 16) -> str:
    """A random password that satisfies the standard complexity policy."""
    while True:
        pw = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        ok, _ = validate_password(pw, complexity="standard")
        if ok:
            return pw
