# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Images (dimensions, format, EXIF) and archives (a listing and their members, within limits)."""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from axiom.extensions.builtins.library.extract.contract import (
    ChildFile,
    ExtractionContext,
    ExtractionResult,
    ExtractorUnavailable,
    jsonable,
)

_EXIF_KEEP = {
    "DateTime", "DateTimeOriginal", "Make", "Model", "LensModel", "Software", "ExposureTime", "FNumber",
    "ISOSpeedRatings", "FocalLength", "ImageDescription", "Artist", "Orientation",
}


def image(ctx: ExtractionContext) -> ExtractionResult:
    try:
        from PIL import ExifTags, Image
    except ImportError as exc:
        raise ExtractorUnavailable("Pillow is not installed (pip install pillow)") from exc
    Image.MAX_IMAGE_PIXELS = 400_000_000  # a decompression bomb is refused by Pillow past this
    with Image.open(str(ctx.path)) as im:
        meta: dict[str, Any] = {"format": im.format, "width": im.width, "height": im.height, "mode": im.mode}
        frames = getattr(im, "n_frames", 1)
        if frames > 1:
            meta["frames"] = frames
        exif: dict[str, Any] = {}
        try:
            raw = im.getexif()
            for tag, value in raw.items():
                name = ExifTags.TAGS.get(tag, str(tag))
                if name in _EXIF_KEEP:
                    exif[name] = jsonable(value.decode(errors="replace") if isinstance(value, bytes) else value)
            gps = raw.get_ifd(0x8825) if hasattr(raw, "get_ifd") else {}
            if gps:
                # Location is personal data; recorded as present, not copied.
                exif["GPS"] = "present (not copied into the catalog)"
        except Exception:  # noqa: BLE001 - EXIF is optional and often malformed
            pass
    if exif:
        meta["exif"] = exif
    return ExtractionResult(
        metadata=meta,
        features={"pixels": meta["width"] * meta["height"]},
        summary=f"{meta['format']} image, {meta['width']}x{meta['height']}",
    )


# --------------------------------------------------------------------- archives


def _limit(env: str, default: int) -> int:
    try:
        v = int(os.environ.get(env, "") or default)
        return v if v > 0 else default
    except ValueError:
        return default


def archive_limits() -> dict[str, int]:
    return {
        "depth": _limit("AXIOM_LIBRARY_ARCHIVE_DEPTH", 3),
        "members": _limit("AXIOM_LIBRARY_ARCHIVE_MEMBERS", 2000),
        "bytes": _limit("AXIOM_LIBRARY_ARCHIVE_BYTES", 4 * 1024**3),
        "ratio": _limit("AXIOM_LIBRARY_ARCHIVE_RATIO", 200),
    }


class ArchiveRefused(ValueError):
    pass


def _safe_member(name: str) -> str | None:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    return str(p)


def archive(ctx: ExtractionContext) -> ExtractionResult:
    """List an archive and unpack its members for the pipeline to identify and extract.

    Limits (all environment-tunable): nesting depth, member count, total
    uncompressed bytes and per-member compression ratio. Unsafe paths, links and
    devices are listed and never unpacked. Over a limit, the listing stays and
    the unpacking stops, with the reason as a warning.
    """
    lim = archive_limits()
    lower = ctx.file_name.lower()
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    out_dir = Path(tempfile.mkdtemp(prefix="library-archive-"))
    children: list[ChildFile] = []
    unpack = ctx.depth < lim["depth"]
    if not unpack:
        warnings.append(f"nested {ctx.depth} archives deep; members are listed, not unpacked (limit {lim['depth']})")
    budget = lim["bytes"]
    try:
        if zipfile.is_zipfile(ctx.path):
            with zipfile.ZipFile(ctx.path) as zf:
                infos = zf.infolist()
                if len(infos) > lim["members"]:
                    warnings.append(f"{len(infos):,} members; only the first {lim['members']:,} are considered")
                for info in infos[: lim["members"]]:
                    safe = _safe_member(info.filename)
                    kind = "dir" if info.is_dir() else "file"
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == stat.S_IFLNK:
                        kind = "link"
                    entries.append({"path": info.filename, "size": info.file_size, "kind": kind})
                    if not unpack or kind != "file" or safe is None:
                        if safe is None:
                            warnings.append(f"{info.filename!r}: unsafe path, not unpacked")
                        continue
                    if info.compress_size and info.file_size / info.compress_size > lim["ratio"]:
                        warnings.append(f"{info.filename!r}: decompresses suspiciously far; not unpacked")
                        continue
                    if info.file_size > budget:
                        warnings.append("archive byte budget reached; remaining members listed only")
                        unpack = False
                        continue
                    target = out_dir / safe
                    target.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with zf.open(info) as src, target.open("wb") as dst:
                        while chunk := src.read(1024 * 1024):
                            written += len(chunk)
                            if written > info.file_size + 65536 or written > budget:
                                raise ArchiveRefused(f"{info.filename!r} inflates past what it declares")
                            dst.write(chunk)
                    budget -= written
                    children.append(ChildFile(member=safe, path=target))
            fmt = "zip"
        elif tarfile.is_tarfile(ctx.path):
            with tarfile.open(ctx.path, "r:*") as tf:
                members = tf.getmembers()
                if len(members) > lim["members"]:
                    warnings.append(f"{len(members):,} members; only the first {lim['members']:,} are considered")
                for m in members[: lim["members"]]:
                    safe = _safe_member(m.name)
                    kind = "dir" if m.isdir() else "file" if m.isfile() else "link" if (m.issym() or m.islnk()) else "special"
                    entries.append({"path": m.name, "size": m.size, "kind": kind})
                    if not unpack or kind != "file" or safe is None:
                        if safe is None:
                            warnings.append(f"{m.name!r}: unsafe path, not unpacked")
                        continue
                    if m.size > budget:
                        warnings.append("archive byte budget reached; remaining members listed only")
                        unpack = False
                        continue
                    src = tf.extractfile(m)
                    if src is None:
                        continue
                    target = out_dir / safe
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    budget -= m.size
                    children.append(ChildFile(member=safe, path=target))
            fmt = "tar"
        else:
            raise ValueError("not a zip or tar archive")
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    total = sum(e["size"] for e in entries)
    return ExtractionResult(
        metadata={"format": fmt, "members": len(entries), "uncompressed_bytes": total, "unpacked_dir": str(out_dir)},
        features={"members": len(entries)},
        children=children,
        preview={"type": "listing", "entries": entries[:2000]},
        warnings=warnings,
        summary=f"{fmt} archive, {len(entries):,} members ({total:,} bytes)" + ("" if lower else ""),
    )


__all__ = ["ArchiveRefused", "archive", "archive_limits", "image"]
