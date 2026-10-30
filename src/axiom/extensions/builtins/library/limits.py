# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What the library accepts, checked before a byte is written anywhere.

Every check here runs on the bytes and the name the client sent, and none of
them trusts either. The type a file is stored and served as comes from this
module's allowlist, never from the client's ``Content-Type``; the name it is
stored under is reduced to a basename with nothing a path or a terminal could
interpret; and a container format (the office formats are zip files) is opened
and measured before anything decompresses it for real.

The caps are environment-tunable because a node behind a proxy has a body
ceiling the application cannot see. They are served to the client
(``GET /api/v1/library/limits``) so the browser refuses an oversized file with
the server's own numbers instead of discovering the limit as a 413.

A malware scanner is a seam, not a dependency: ``AXIOM_LIBRARY_SCAN_CMD`` names
a command (``clamdscan --no-summary -`` for one) that reads the file on stdin
and exits non-zero to refuse it. Configured and failing is a refusal; not
configured is recorded on the item as ``not-scanned`` so nobody mistakes the
absence of a scanner for a clean result.
"""

from __future__ import annotations

import io
import os
import re
import shlex
import subprocess
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

MiB = 1024 * 1024

#: Per-file ceiling. Generous for documents; a dataset belongs in the data platform.
DEFAULT_MAX_FILE_BYTES = 100 * MiB
#: Files one upload request may carry.
DEFAULT_MAX_FILES = 20
#: Bytes one upload request may carry in total.
DEFAULT_MAX_BATCH_BYTES = 200 * MiB

#: A container's members, summed and uncompressed. A zip bomb is small on the
#: wire and enormous on disk; this is the number that catches it.
MAX_UNCOMPRESSED_BYTES = 512 * MiB
#: Members one container may hold.
MAX_ARCHIVE_MEMBERS = 10_000
#: Uncompressed-to-compressed ratio past which a member is refused outright.
#: Real office documents compress text around 10:1; a bomb is in the thousands.
MAX_COMPRESSION_RATIO = 200

_ENV_FILE = "AXIOM_LIBRARY_MAX_FILE_BYTES"
_ENV_FILES = "AXIOM_LIBRARY_MAX_FILES"
_ENV_BATCH = "AXIOM_LIBRARY_MAX_BATCH_BYTES"
SCAN_ENV = "AXIOM_LIBRARY_SCAN_CMD"


class UploadRefused(ValueError):
    """A file the library will not take, with the reason a person can act on.

    ``status`` is the HTTP answer: 413 for size, 415 for type, 422 for a file
    whose content contradicts its name or that a guard refused.
    """

    def __init__(self, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class FileType:
    """One accepted kind of file: how it is stored, served and indexed."""

    mime: str
    #: Shown inline in the browser (preview) rather than only downloaded.
    inline: bool
    #: Extracted, chunked and embedded into the retrieval corpus.
    indexable: bool
    #: How to check the bytes agree with the name: ``text``, ``pdf``, ``ooxml``,
    #: ``ole``, ``odf`` or an image signature name.
    sniff: str


#: The allowlist, by lowercase extension. Anything absent is refused (415).
#: SVG and HTML are absent on purpose: both are documents that run script in
#: the viewer's origin, and a library that previews them is a stored-XSS
#: endpoint with a nice icon.
ACCEPTED: dict[str, FileType] = {
    ".pdf": FileType("application/pdf", True, True, "pdf"),
    ".txt": FileType("text/plain", True, True, "text"),
    ".md": FileType("text/markdown", True, True, "text"),
    ".rst": FileType("text/x-rst", True, True, "text"),
    ".csv": FileType("text/csv", True, True, "text"),
    ".tsv": FileType("text/tab-separated-values", True, True, "text"),
    ".json": FileType("application/json", True, True, "text"),
    ".log": FileType("text/plain", True, True, "text"),
    ".xml": FileType("application/xml", False, True, "text"),
    ".docx": FileType(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", False, True, "ooxml"
    ),
    ".xlsx": FileType("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", False, True, "ooxml"),
    ".pptx": FileType(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", False, True, "ooxml"
    ),
    ".odt": FileType("application/vnd.oasis.opendocument.text", False, True, "odf"),
    ".doc": FileType("application/msword", False, True, "ole"),
    ".png": FileType("image/png", True, False, "png"),
    ".jpg": FileType("image/jpeg", True, False, "jpeg"),
    ".jpeg": FileType("image/jpeg", True, False, "jpeg"),
    ".gif": FileType("image/gif", True, False, "gif"),
    ".webp": FileType("image/webp", True, False, "webp"),
}

_SIGNATURES = {
    "pdf": (b"%PDF-",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
    "ooxml": (b"PK\x03\x04",),
    "odf": (b"PK\x03\x04",),
    "ole": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "zip": (b"PK\x03\x04", b"PK\x05\x06"),
    "gzip": (b"\x1f\x8b",),
    "parquet": (b"PAR1",),
    "hdf5": (b"\x89HDF\r\n\x1a\n",),
    "tiff": (b"II*\x00", b"MM\x00*"),
    "bmp": (b"BM",),
}

#: Files with no extension (``README``, a simulation's ``outp``) are accepted
#: when their bytes are UTF-8 text, and stored and served as plain text.
EXTENSIONLESS_TEXT = FileType("text/plain", True, True, "text")

#: The largest file a resumable upload session takes (multipart keeps the
#: per-file cap above): large scientific files belong in bronze too.
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024**3
_ENV_UPLOAD = "AXIOM_LIBRARY_MAX_UPLOAD_BYTES"


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def max_file_bytes() -> int:
    return _env_int(_ENV_FILE, DEFAULT_MAX_FILE_BYTES)


def max_files() -> int:
    return _env_int(_ENV_FILES, DEFAULT_MAX_FILES)


def max_batch_bytes() -> int:
    return _env_int(_ENV_BATCH, DEFAULT_MAX_BATCH_BYTES)


def max_upload_bytes() -> int:
    return _env_int(_ENV_UPLOAD, DEFAULT_MAX_UPLOAD_BYTES)


def accepted() -> dict[str, FileType]:
    """The allowlist: the library's own types plus what installed extractors add.

    A contributed type can never replace a built-in one (so no extension can
    make ``.pdf`` mean something else), and never makes a scripting document
    previewable inline: SVG and HTML stay refused whoever asks.
    """
    out = dict(ACCEPTED)
    try:
        from axiom.extensions.builtins.library.extract.registry import accepted_types

        for ext, a in accepted_types().items():
            if ext in out or ext in _NEVER:
                continue
            out[ext] = FileType(a.mime, bool(a.inline), True, a.sniff)
    except Exception:  # noqa: BLE001 - a broken registry leaves the built-in list
        pass
    return out


#: Never accepted, whatever an extension declares: they run script in the viewer.
_NEVER = frozenset({".svg", ".svgz", ".html", ".htm", ".xhtml", ".js", ".mjs", ".hta", ".swf"})


def effective_limits() -> dict:
    """The numbers the server enforces, for the client to check against first."""
    return {
        "max_file_bytes": max_file_bytes(),
        "max_files": max_files(),
        "max_batch_bytes": max_batch_bytes(),
        "max_upload_bytes": max_upload_bytes(),
        "accepted_extensions": sorted(accepted()),
        "extensionless": "text",
        "scanner": "configured" if (os.environ.get(SCAN_ENV) or "").strip() else "not-configured",
    }


def format_bytes(n: int) -> str:
    if n >= MiB:
        return f"{n // MiB} MB"
    if n >= 1024:
        return f"{n // 1024} KB"
    return f"{n} B"


_UNSAFE = re.compile(r"[^\w.\- ()+,@]")


def safe_file_name(raw: str | None) -> str:
    """The name a file is stored and shown under, or a refusal.

    The basename only, whichever separator the client used: ``../../x`` and
    ``C:\\x`` both become ``x``. Control characters, separators and anything a
    shell or a header could interpret are replaced, a leading dot is dropped (a
    library has no hidden files), and the length is capped with the extension
    kept. A name with nothing left is refused rather than invented.
    """
    name = unicodedata.normalize("NFC", str(raw or ""))
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = _UNSAFE.sub("_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise UploadRefused("the file has no usable name", status=422)
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) <= 10:
        stem = stem[: 200 - len(ext) - 1]
        name = f"{stem}.{ext}" if stem else f"file.{ext}"
    else:
        name = name[:200]
    return name


def file_type_for(name: str) -> FileType:
    ext = PurePosixPath(name).suffix.lower()
    if not ext:
        return EXTENSIONLESS_TEXT
    table = accepted()
    found = table.get(ext)
    if found is None and _looks_like_version_suffix(name):
        return EXTENSIONLESS_TEXT
    if found is None:
        allowed = ", ".join(sorted(table))
        raise UploadRefused(
            f"{name!r}: files of type {ext or '(none)'} are not accepted. Accepted: {allowed}",
            status=415,
        )
    return found


def _looks_like_version_suffix(name: str) -> bool:
    """``run.001``, ``deck.v2``: a numeric or version-like suffix is not a type."""
    ext = PurePosixPath(name).suffix.lower().lstrip(".")
    return bool(re.fullmatch(r"v?\d{1,4}", ext))


def check_size(name: str, size: int, *, cap: int | None = None) -> None:
    if size <= 0:
        raise UploadRefused(f"{name!r} is empty", status=422)
    cap = cap or max_file_bytes()
    if size > cap:
        raise UploadRefused(
            f"{name!r} is too large ({format_bytes(size)}); the most this library takes is {format_bytes(cap)}",
            status=413,
        )


def check_batch(sizes: list[int]) -> None:
    if len(sizes) > max_files():
        raise UploadRefused(f"at most {max_files()} files per upload", status=413)
    total = sum(sizes)
    if total > max_batch_bytes():
        raise UploadRefused(
            f"this upload is {format_bytes(total)}; the most one upload may carry is "
            f"{format_bytes(max_batch_bytes())}. Send fewer files at a time.",
            status=413,
        )


def _check_signature(name: str, kind: FileType, content: bytes) -> None:
    if kind.sniff == "text":
        if b"\x00" in content[:8192]:
            raise UploadRefused(f"{name!r} is named as text but contains binary data")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise UploadRefused(f"{name!r} is named as text but is not UTF-8") from None
        return
    if kind.sniff == "binary":
        return
    if kind.sniff == "webp":
        if not (content[:4] == b"RIFF" and content[8:12] == b"WEBP"):
            raise UploadRefused(f"{name!r} does not contain what its name says")
        return
    sigs = _SIGNATURES.get(kind.sniff, ())
    if sigs and not any(content.startswith(s) for s in sigs):
        raise UploadRefused(f"{name!r} does not contain what its name says")


def _check_container(name: str, content: bytes) -> None:
    """Measure a zip-based document without extracting it.

    The central directory states every member's sizes, so the bomb is caught
    from the declaration. The declaration can lie, so each member is then
    streamed with a hard stop at the declared size plus one block: a member
    that inflates past what it claimed is refused mid-read.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise UploadRefused(f"{name!r} is not a valid document (its container is damaged)") from None
    with archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise UploadRefused(f"{name!r} holds {len(members)} parts; refused as a likely archive bomb")
        declared = 0
        for info in members:
            if info.filename.startswith("/") or ".." in PurePosixPath(info.filename).parts:
                raise UploadRefused(f"{name!r} contains a part with an unsafe path")
            declared += info.file_size
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise UploadRefused(f"{name!r} decompresses suspiciously far; refused as a likely archive bomb")
        if declared > MAX_UNCOMPRESSED_BYTES:
            raise UploadRefused(f"{name!r} would decompress to {format_bytes(declared)}; refused")
        budget = MAX_UNCOMPRESSED_BYTES
        for info in members:
            if info.is_dir():
                continue
            read = 0
            with archive.open(info) as stream:
                while chunk := stream.read(64 * 1024):
                    read += len(chunk)
                    budget -= len(chunk)
                    if read > info.file_size + 64 * 1024 or budget < 0:
                        raise UploadRefused(f"{name!r} inflates past what it declares; refused")


def scan(name: str, content: bytes) -> str:
    """Run the configured scanner, if any. Returns ``clean`` or ``not-scanned``."""
    cmd = (os.environ.get(SCAN_ENV) or "").strip()
    if not cmd:
        return "not-scanned"
    try:
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- the command is the operator's own deployment setting (SCAN_ENV), split without a shell; the upload supplies only stdin
        done = subprocess.run(  # noqa: S603 - an operator-configured command, no shell
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            shlex.split(cmd), input=content, capture_output=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # A scanner that is configured and cannot run refuses: the operator
        # asked for files to be scanned, and an unscanned file is not that.
        raise UploadRefused(f"{name!r} could not be scanned ({type(exc).__name__}); refused") from None
    if done.returncode != 0:
        raise UploadRefused(f"{name!r} was refused by the malware scanner")
    return "clean"


def _check_signature_head(name: str, kind: FileType, head: bytes, path) -> None:
    if kind.sniff == "text":
        if b"\x00" in head[:8192]:
            raise UploadRefused(f"{name!r} is named as text but contains binary data")
        # The whole file must be UTF-8; checked streaming, so a large log costs a read, not memory.
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")()
        with open(path, "rb") as f:
            try:
                while chunk := f.read(1024 * 1024):
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                raise UploadRefused(f"{name!r} is named as text but is not UTF-8") from None
        return
    _check_signature(name, kind, head)


def scan_path(name: str, path) -> str:
    """The scanner on a file on disk, streamed to its stdin."""
    cmd = (os.environ.get(SCAN_ENV) or "").strip()
    if not cmd:
        return "not-scanned"
    try:
        with open(path, "rb") as f:
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- the command is the operator's own deployment setting (SCAN_ENV), split without a shell; the upload supplies only stdin
            done = subprocess.run(  # noqa: S603 - an operator-configured command, no shell
                # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
                shlex.split(cmd), stdin=f, capture_output=True, timeout=1800, check=False
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UploadRefused(f"{name!r} could not be scanned ({type(exc).__name__}); refused") from None
    if done.returncode != 0:
        raise UploadRefused(f"{name!r} was refused by the malware scanner")
    return "clean"


def check_declared(raw_name: str | None, size: int) -> tuple[str, FileType]:
    """The guards a resumable session runs on its declaration, before any byte is kept."""
    name = safe_file_name(raw_name)
    kind = file_type_for(name)
    check_size(name, size, cap=max_upload_bytes())
    return name, kind


def validate_path(raw_name: str | None, path, *, cap: int | None = None) -> tuple[str, FileType, str]:
    """Every guard on a file on disk (any size): ``(safe name, file type, scan result)``."""
    from pathlib import Path

    p = Path(path)
    name = safe_file_name(raw_name)
    kind = file_type_for(name)
    size = p.stat().st_size
    check_size(name, size, cap=cap or max_upload_bytes())
    with p.open("rb") as f:
        head = f.read(65536)
    _check_signature_head(name, kind, head, p)
    if kind.sniff in ("ooxml", "odf"):
        _check_container(name, p.read_bytes())
    return name, kind, scan_path(name, p)


def validate(raw_name: str | None, content: bytes) -> tuple[str, FileType, str]:
    """Every guard, in order: ``(safe name, file type, scan result)`` or a refusal."""
    name = safe_file_name(raw_name)
    kind = file_type_for(name)
    check_size(name, len(content))
    _check_signature(name, kind, content)
    if kind.sniff in ("ooxml", "odf"):
        _check_container(name, content)
    return name, kind, scan(name, content)


__all__ = [
    "ACCEPTED",
    "FileType",
    "SCAN_ENV",
    "UploadRefused",
    "accepted",
    "check_batch",
    "check_declared",
    "check_size",
    "effective_limits",
    "file_type_for",
    "format_bytes",
    "max_batch_bytes",
    "max_file_bytes",
    "max_files",
    "max_upload_bytes",
    "scan_path",
    "validate_path",
    "safe_file_name",
    "scan",
    "validate",
]
