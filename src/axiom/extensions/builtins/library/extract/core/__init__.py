# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The generic extractors Axiom ships (ADR-213 D5).

Registered in code rather than discovered, so every install has them however
discovery fares. They are declared exactly as a contributed extractor is (the
same manifest fields), which keeps the core honest about the contract.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.library.extract.contract import ExtractorManifest, manifest_from_dict
from axiom.extensions.builtins.library.extract.core import (
    documents,
    media,
    scientific,
    tabular,
    text,
)

_CODE = sorted(text.CODE_EXTENSIONS)

CORE: list[tuple[dict[str, Any], Any]] = [
    (
        {
            "name": "core.extract.file_facts",
            "version": "1.0.0",
            "summary": "Size, checksum, type and encoding: what every file has",
            "outputs": ["metadata"],
            "ec": "none",
            "always": True,
            "priority": -100,
            "match": {"names": ["*"]},
            "limits": {"max_bytes": 10**15, "timeout_s": 5},
        },
        text.facts,
    ),
    (
        {
            "name": "core.extract.text",
            "version": "1.0.0",
            "summary": "Plain text, Markdown, reStructuredText, logs and source code",
            "outputs": ["metadata", "features", "text"],
            "tags": ["rag"],
            "match": {
                "extensions": [".txt", ".md", ".markdown", ".rst", ".log", ".tex", ".toml", ".ini", ".cfg", *_CODE],
                "mime": ["text/plain", "text/markdown", "text/x-rst"],
            },
            "accepts": [
                *({"extension": e, "mime": "text/plain", "sniff": "text"} for e in [".markdown", ".tex", ".toml", ".ini", ".cfg", *_CODE]),
            ],
            "limits": {"timeout_s": 30},
        },
        text.plain_text,
    ),
    (
        {
            "name": "core.extract.structured",
            "version": "1.0.0",
            "summary": "JSON, JSON lines and YAML: structure, keys and text",
            "outputs": ["metadata", "features", "text"],
            "tags": ["rag"],
            "match": {"extensions": [".json", ".jsonl", ".ndjson", ".yaml", ".yml"], "mime": ["application/json"]},
            "accepts": [
                {"extension": e, "mime": m, "sniff": "text", "inline": False}
                for e, m in ((".jsonl", "application/x-ndjson"), (".ndjson", "application/x-ndjson"), (".yaml", "application/yaml"), (".yml", "application/yaml"))
            ],
            "limits": {"max_bytes": 64 * 1024**2, "timeout_s": 30},
        },
        text.structured,
    ),
    (
        {
            "name": "core.extract.xml",
            "version": "1.0.0",
            "summary": "XML: root, element counts and text (no entity expansion)",
            "outputs": ["metadata", "features", "text"],
            "tags": ["rag"],
            "match": {"extensions": [".xml"], "mime": ["application/xml", "text/xml"]},
            "limits": {"max_bytes": 64 * 1024**2, "timeout_s": 30},
        },
        text.xml,
    ),
    (
        {
            "name": "core.extract.pdf",
            "version": "1.0.0",
            "summary": "PDF: text (with table recovery), pages and document properties",
            "outputs": ["metadata", "features", "text"],
            "tags": ["rag"],
            "match": {"extensions": [".pdf"], "magic": ["%PDF-"], "mime": ["application/pdf"]},
            "limits": {"max_bytes": 512 * 1024**2, "timeout_s": 180},
        },
        documents.pdf,
    ),
    (
        {
            "name": "core.extract.office",
            "version": "1.0.0",
            "summary": "Word, PowerPoint and OpenDocument text: text and core properties",
            "outputs": ["metadata", "text"],
            "tags": ["rag"],
            "match": {"extensions": [".docx", ".pptx", ".odt", ".doc"]},
            "limits": {"max_bytes": 512 * 1024**2, "timeout_s": 120},
        },
        documents.office,
    ),
    (
        {
            "name": "core.extract.workbook",
            "version": "1.0.0",
            "summary": "Excel workbooks: every sheet as a table, and text",
            "outputs": ["metadata", "tables", "text"],
            "tags": ["rag", "dataset"],
            "match": {"extensions": [".xlsx", ".xlsm"]},
            "requires": ["openpyxl"],
            "limits": {"max_bytes": 512 * 1024**2, "timeout_s": 120},
        },
        documents.workbook,
    ),
    (
        {
            "name": "core.extract.delimited",
            "version": "1.0.0",
            "summary": "CSV and TSV: columns, types, a sample, the row count, and series shape",
            "outputs": ["metadata", "features", "tables", "text", "preview"],
            "tags": ["dataset", "rag"],
            "match": {"extensions": [".csv", ".tsv"], "mime": ["text/csv", "text/tab-separated-values"]},
            "limits": {"max_bytes": 64 * 1024**3, "timeout_s": 900},
        },
        tabular.delimited,
    ),
    (
        {
            "name": "core.extract.parquet",
            "version": "1.0.0",
            "summary": "Parquet: schema, row count, row groups and a sample",
            "outputs": ["metadata", "features", "tables"],
            "tags": ["dataset"],
            "ec": "none",
            "match": {"extensions": [".parquet", ".pq"], "magic": ["PAR1"]},
            "accepts": [
                {"extension": ".parquet", "mime": "application/vnd.apache.parquet", "sniff": "parquet"},
                {"extension": ".pq", "mime": "application/vnd.apache.parquet", "sniff": "parquet"},
            ],
            "requires": ["pyarrow"],
            "limits": {"max_bytes": 64 * 1024**3, "timeout_s": 60},
        },
        tabular.parquet,
    ),
    (
        {
            "name": "core.extract.hdf5",
            "version": "1.0.0",
            "summary": "HDF5 and NetCDF-4: groups, datasets, shapes, types, attributes and peeks",
            "outputs": ["metadata", "features", "preview"],
            "ec": "none",
            "match": {"extensions": [".h5", ".hdf5", ".he5", ".nc", ".nc4"], "magic": ["hex:894844460d0a1a0a"]},
            "accepts": [
                {"extension": e, "mime": "application/x-hdf5", "sniff": "binary"}
                for e in (".h5", ".hdf5", ".he5", ".nc4")
            ]
            + [{"extension": ".nc", "mime": "application/x-netcdf", "sniff": "binary"}],
            "requires": ["h5py"],
            "limits": {"max_bytes": 10**15, "timeout_s": 60},
        },
        scientific.hdf5,
    ),
    (
        {
            "name": "core.extract.netcdf3",
            "version": "1.0.0",
            "summary": "Classic NetCDF: dimensions, variables and attributes",
            "outputs": ["metadata", "features", "preview"],
            "ec": "none",
            "match": {"magic": ["CDF\x01", "CDF\x02"]},
            "requires": ["netCDF4"],
            "limits": {"max_bytes": 10**15, "timeout_s": 60},
        },
        scientific.netcdf3,
    ),
    (
        {
            "name": "core.extract.image",
            "version": "1.0.0",
            "summary": "Images: format, dimensions and EXIF (location noted, never copied)",
            "outputs": ["metadata", "features"],
            "ec": "none",
            "match": {"extensions": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"], "mime": ["image/*"]},
            "accepts": [
                {"extension": ".tif", "mime": "image/tiff", "sniff": "tiff"},
                {"extension": ".tiff", "mime": "image/tiff", "sniff": "tiff"},
                {"extension": ".bmp", "mime": "image/bmp", "sniff": "bmp"},
            ],
            "requires": ["PIL"],
            "limits": {"max_bytes": 2 * 1024**3, "timeout_s": 30},
        },
        media.image,
    ),
    (
        {
            "name": "core.extract.archive",
            "version": "1.0.0",
            "summary": "Zip and tar archives: a listing, and every member identified and extracted in turn",
            "outputs": ["metadata", "features", "preview"],
            "ec": "none",
            "match": {"extensions": [".zip", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"]},
            "accepts": [
                {"extension": ".zip", "mime": "application/zip", "sniff": "zip"},
                {"extension": ".tar", "mime": "application/x-tar", "sniff": "binary"},
                {"extension": ".tgz", "mime": "application/gzip", "sniff": "gzip"},
                {"extension": ".gz", "mime": "application/gzip", "sniff": "gzip"},
                {"extension": ".tbz2", "mime": "application/x-bzip2", "sniff": "binary"},
                {"extension": ".bz2", "mime": "application/x-bzip2", "sniff": "binary"},
                {"extension": ".txz", "mime": "application/x-xz", "sniff": "binary"},
                {"extension": ".xz", "mime": "application/x-xz", "sniff": "binary"},
            ],
            "limits": {"max_bytes": 16 * 1024**3, "timeout_s": 600},
        },
        media.archive,
    ),
]

#: Media the Library previews but no extractor reads beyond the universal facts.
MEDIA_TYPES = [
    {"extension": ".mp4", "mime": "video/mp4", "sniff": "binary", "inline": True},
    {"extension": ".webm", "mime": "video/webm", "sniff": "binary", "inline": True},
    {"extension": ".mov", "mime": "video/quicktime", "sniff": "binary", "inline": True},
    {"extension": ".mp3", "mime": "audio/mpeg", "sniff": "binary", "inline": True},
    {"extension": ".wav", "mime": "audio/wav", "sniff": "binary", "inline": True},
    {"extension": ".ogg", "mime": "audio/ogg", "sniff": "binary", "inline": True},
    {"extension": ".m4a", "mime": "audio/mp4", "sniff": "binary", "inline": True},
    {"extension": ".flac", "mime": "audio/flac", "sniff": "binary", "inline": True},
]

_built: list[tuple[ExtractorManifest, Any]] | None = None


def core_extractors() -> list[tuple[ExtractorManifest, Any]]:
    global _built
    if _built is None:
        out = []
        for data, fn in CORE:
            if data["name"] == "core.extract.file_facts":
                data = {**data, "accepts": MEDIA_TYPES}
            out.append((manifest_from_dict(data, scope="core", contributed_by="library"), fn))
        _built = out
    return list(_built)


__all__ = ["CORE", "MEDIA_TYPES", "core_extractors"]
