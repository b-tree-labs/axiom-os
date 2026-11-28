# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``<cli> setup audit-key``: give this node the key that signs its audit chain (#2470).

The key is made here and written to one file only its owner can read. It is
never printed. Every Axiom service reads it when it starts, so services
already running sign nothing until they are restarted.

A node that already has a key keeps it: a new key cannot verify the records
the old one signed.
"""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

from .environment import cli_name


def write_key(path: Path) -> bool:
    """Write a new key to ``path``, owner-only. False when the node already has one."""
    from axiom.infra.audit_log import KEY_VARIABLE

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        # Created owner-only and only if absent: the key never exists in a readable file.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Signs this node's audit chain. Keep it; back it up; never share it.\n")
        f.write(f"{KEY_VARIABLE}={secrets.token_hex(32)}\n")
    return True


def main(argv: list[str] | None = None) -> int:
    from axiom.infra import audit_log

    cli = cli_name()
    argparse.ArgumentParser(
        prog=f"{cli} setup audit-key",
        description=(
            "Give this node the key that signs its audit chain. The key is written to a file "
            "only you can read and is never shown."
        ),
    ).parse_args(argv)

    path = audit_log.key_file()
    if os.environ.get(audit_log.KEY_VARIABLE):
        print(
            f"This node already has an audit key, from {audit_log.KEY_VARIABLE} in the environment."
        )
        print("Nothing was changed.")
        return 0
    if audit_log.signing_key():
        print(f"This node already has an audit key, in {path}. Nothing was changed.")
        return 0
    if path.exists():
        print(f"{path} exists but holds no usable key (empty, or readable by other accounts).")
        print(f"Fix: chmod 600 {path}, or move it aside and run this again.")
        return 1

    from axiom.infra.change_intent import record_change

    with record_change("config", "audit-key", detail=f"audit signing key written to {path}"):
        write_key(path)
    print(f"Audit key written to {path} (only you can read it; it was not shown).")
    print(f"Audit records are kept in {audit_log.default_log_dir()}.")
    print("Restart every running service of this node: each reads the key when it starts.")
    print(
        "Back the key file up with the node's secrets. Without it the records cannot be verified."
    )
    return 0
