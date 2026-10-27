# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A kit as one content-addressed bundle (ADR-203).

Promotion sends declarations, not a release: ``kit.toml`` and the ``gold``,
``verbs``, ``charts``, ``derived`` and ``automations`` folders, plus any
``conform`` code so the node can say plainly that it does not run it yet. An
automation is TOML and nothing else: a file of any other kind under
``automations/`` is refused at both ends, packing and unpacking. Samples and the kit's own
state never travel: they are the contributor's data and workbench.

The archive is deterministic (sorted names, zero times and owners), so the same
kit always has the same hash, and a node can tell a resubmission from a change.
Unpacking refuses anything but plain files under the archive's root.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .project import MANIFEST, KitError, KitProject

#: What a bundle carries. Everything else in the kit folder stays behind.
DECLARATION_DIRS = ("gold", "verbs", "charts", "derived", "automations")
#: Folders that may hold TOML declarations only. Python beside an automation
#: would be code the node is asked to schedule, so it never travels.
TOML_ONLY_DIRS = ("automations",)
CODE_DIRS = ("conform",)
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_FILES = 500


@dataclass(frozen=True)
class Bundle:
    tenant: str
    data: bytes
    sha256: str
    files: tuple[str, ...]
    has_code: bool

    def manifest(self) -> dict:
        return {
            "tenant": self.tenant,
            "sha256": self.sha256,
            "files": list(self.files),
            "has_code": self.has_code,
            "bytes": len(self.data),
        }


def _toml_only_problem(name: str) -> str | None:
    parts = PurePosixPath(name).parts
    if len(parts) > 1 and parts[0] in TOML_ONLY_DIRS and not (len(parts) == 2 and name.endswith(".toml")):
        return (f"the bundle holds {name!r}: {parts[0]}/ carries one .toml declaration per automation "
                f"and nothing else (no code, no subfolders)")
    return None


def _members(project: KitProject) -> list[tuple[str, Path]]:
    root = project.root
    picked = [(MANIFEST, root / MANIFEST)]
    for folder in (*DECLARATION_DIRS, *CODE_DIRS):
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name.startswith(".") or "__pycache__" in path.parts:
                continue
            picked.append((path.relative_to(root).as_posix(), path))
    return sorted(picked)


def pack(project: KitProject) -> Bundle:
    """The kit's declarations (and any code) as one deterministic ``.tar.gz``."""
    members = _members(project)
    for name, _ in members:
        why = _toml_only_problem(name)
        if why:
            raise KitError(why)
    if len(members) > MAX_FILES:
        raise KitError(f"the kit has {len(members)} files to send; the limit is {MAX_FILES}")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, path in members:
            content = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(content), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(content))
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", mtime=0) as out:
        out.write(raw.getvalue())
    data = gz.getvalue()
    if len(data) > MAX_BUNDLE_BYTES:
        raise KitError(f"the bundle is {len(data)} bytes; the limit is {MAX_BUNDLE_BYTES}")
    names = tuple(name for name, _ in members)
    return Bundle(
        tenant=project.tenant,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        files=names,
        has_code=any(n.split("/", 1)[0] in CODE_DIRS for n in names),
    )


def unpack(data: bytes, dest: Path) -> Path:
    """Write a bundle under ``dest`` and return the kit root. Plain files only."""
    if len(data) > MAX_BUNDLE_BYTES:
        raise KitError(f"the bundle is {len(data)} bytes; the limit is {MAX_BUNDLE_BYTES}")
    dest = Path(dest)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            if (
                not member.isfile()
                or name.is_absolute()
                or ".." in name.parts
                or name.parts[0] not in (MANIFEST, *DECLARATION_DIRS, *CODE_DIRS)
            ):
                raise KitError(f"the bundle holds {member.name!r}, which a kit bundle may not")
            why = _toml_only_problem(name.as_posix())
            if why:
                raise KitError(why)
            count += 1
            if count > MAX_FILES:
                raise KitError(f"the bundle holds more than {MAX_FILES} files")
            target = dest.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            target.write_bytes(extracted.read() if extracted else b"")
    if not (dest / MANIFEST).is_file():
        raise KitError(f"the bundle has no {MANIFEST}")
    return dest


__all__ = ["Bundle", "CODE_DIRS", "DECLARATION_DIRS", "TOML_ONLY_DIRS", "pack", "unpack"]
