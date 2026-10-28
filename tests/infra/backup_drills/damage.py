# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Local damage helpers for the restore drills.

``tests/chaos/state_chaos`` (branch ``test/state-chaos``) is not on this base and branches are not
merged here, so these are the few primitives the drills need, written for backups specifically:
every function damages a file or directory in a way real storage does, and none touches anything
outside the path it is given.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path


def bit_flip(path: Path, at: float = 0.5) -> None:
    """Flip every bit of one byte near *at* (0..1) of the file: bit rot."""
    b = bytearray(path.read_bytes())
    i = min(len(b) - 1, max(0, int(len(b) * at)))
    b[i] ^= 0xFF
    path.write_bytes(bytes(b))


def truncate(path: Path, fraction: float = 0.5) -> None:
    """Cut the file short: an interrupted copy or a full disk."""
    data = path.read_bytes()
    path.write_bytes(data[: max(1, int(len(data) * fraction))])


def zero_length(path: Path) -> None:
    path.write_bytes(b"")


def edit_json(path: Path, mutate: Callable[[dict], None]) -> None:
    """Edit a JSON file the way a person (or an attacker) would: change a value, fix nothing else."""
    data = json.loads(path.read_text("utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, indent=1, sort_keys=True))


def rewrite_ledger(path: Path, fn: Callable[[list[dict]], list[dict]], *, rehash: bool = True) -> None:
    """Rewrite a hash-chained ledger; with ``rehash`` the chain is made self-consistent again
    (the forger who knows the format), without it the edit is left raw (the careless one)."""
    from axiom.infra import backup_core as bc

    entries = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    entries = fn(entries)
    if rehash:
        prev = ""
        for i, e in enumerate(entries, 1):
            e["seq"], e["prev"] = i, prev
            e.pop("hash", None)
            e["hash"] = bc.sha256_bytes(bc.canon(e))
            prev = e["hash"]
    path.write_text("\n".join(bc.canon(e).decode() for e in entries) + "\n")


def make_unreadable(path: Path) -> None:
    os.chmod(path, 0)


def make_readable(path: Path) -> None:
    os.chmod(path, 0o700 if path.is_dir() else 0o600)


def read_only_dir(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IXUSR)


def replace_with_file(path: Path) -> None:
    """A directory that something replaced with a plain file."""
    shutil.rmtree(path)
    path.write_text("not a directory any more")


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, symlinks=True)
