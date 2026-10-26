# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.edge_bundle``: write the ingest edge's deploy folder from the installed package.

The person who deploys an ingest edge (ADR-177) is on someone else's network and
has no checkout; what they have is PyPI. This writes the folder the edge's README
walks through (Dockerfile, compose file, nginx blocks, the operator's script),
pinned to the release that wrote it, so there is no file to transfer and no pin
to edit by hand. ``tar`` makes a reproducible ``.tar.gz`` of it as well.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

#: A published release number and nothing else. This is what ends up in a
#: Dockerfile ``RUN pip install``, so it is also the only thing allowed there.
_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")
_MARKER = "BUNDLE"
_FOLDER = "ingest-edge"


def _source() -> Path:
    return Path(__file__).resolve().parents[1] / "deploy" / "ingest-edge" / "docker"


def _installed_version() -> str:
    import importlib.metadata as md

    try:
        return md.version("axiom-os-lm")
    except md.PackageNotFoundError:
        return "unknown"


def _stamp(name: str, text: str, version: str) -> str:
    if name == "Dockerfile":
        text, n = re.subn(r"^ARG AXIOM_VERSION=\S+$", f"ARG AXIOM_VERSION={version}", text, flags=re.M)
    elif name == "compose.yaml":
        text, n = re.subn(r"^(\s*image: axiom-ingest-edge:)\S+$", rf"\g<1>{version}", text, flags=re.M)
    else:
        return text
    if n != 1:
        raise ValueError(f"{name}: expected one version pin to stamp, found {n}")
    return text


def _tarball(folder: Path, dest: Path) -> str:
    """The same release always gives the same bytes: fixed order, owner and times."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        top = tarfile.TarInfo(_FOLDER)
        top.type, top.mode, top.mtime = tarfile.DIRTYPE, 0o755, 0
        tar.addfile(top)
        for p in sorted(folder.iterdir()):
            data = p.read_bytes()
            info = tarfile.TarInfo(f"{_FOLDER}/{p.name}")
            info.size, info.mtime, info.uid, info.gid = len(data), 0, 0, 0
            info.uname = info.gname = ""
            info.mode = 0o755 if p.stat().st_mode & 0o100 else 0o644
            tar.addfile(info, io.BytesIO(data))
    blob = io.BytesIO()
    with gzip.GzipFile(fileobj=blob, mode="wb", mtime=0, filename="") as gz:
        gz.write(raw.getvalue())
    dest.write_bytes(blob.getvalue())
    return hashlib.sha256(blob.getvalue()).hexdigest()


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    version = str(params.get("version") or "").strip() or _installed_version()
    if not _RELEASE.match(version):
        return SkillResult(
            ok=False,
            errors=[
                f"this install is {version!r}, which is not a published release, so a bundle "
                "pinned to it could not be built from PyPI. Pass --version <release> (for "
                "example 0.70.0) to say which release the container should install."
            ],
        )
    out = Path(str(params.get("out") or _FOLDER)).expanduser()
    if out.exists() and any(out.iterdir()) and not (out / _MARKER).exists() and not params.get("force"):
        return SkillResult(
            ok=False,
            errors=[f"{out} is not empty and was not made by this command; choose another --out, or pass --force"],
        )
    src = _source()
    names = sorted(p.name for p in src.iterdir() if p.is_file() and p.name != ".gitignore")
    if not names or "Dockerfile" not in names:
        return SkillResult(ok=False, errors=[f"the bundle's files are missing from this install ({src})"])
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.iterdir():
        if stale.is_file() and (out / _MARKER).exists():
            stale.unlink()
    for name in names:
        text = _stamp(name, (src / name).read_text(encoding="utf-8"), version)
        target = out / name
        target.write_text(text, encoding="utf-8")
        shutil.copymode(src / name, target)
    (out / _MARKER).write_text(f"axiom-os-lm {version}\nwritten by: axi data edge-bundle\n", encoding="utf-8")

    value: dict[str, Any] = {"folder": str(out.resolve()), "version": version, "files": names + [_MARKER]}
    actions = [f"wrote {len(names) + 1} files to {out.resolve()}, pinned to axiom-os-lm=={version}"]
    if params.get("tar"):
        tarball = out.resolve().parent / f"{_FOLDER}-{version}.tar.gz"
        value["sha256"] = _tarball(out, tarball)
        value["tarball"] = str(tarball)
        actions.append(f"wrote {tarball} (sha256 {value['sha256']})")
    actions.append(f"next: copy the folder to the VM and follow {out.resolve() / 'README.md'}")
    return SkillResult(ok=True, value=value, actions_taken=actions)
