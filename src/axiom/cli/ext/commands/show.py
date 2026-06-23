# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext show <name>[@<version>]`` — detailed metadata for an extension.

Consults the local-filesystem registry (or, with ``--installed``, the
install-state records). Reports manifest fields, publish attestation, and
a signature-verification status (``verified`` / ``key unknown`` /
``invalid``) that reuses :mod:`axiom.cli.ext.signing`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.cli.ext._output import console, next_steps
from axiom.cli.ext.install_state import get_installed
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.registry_backend import (
    ArtifactRecord,
    read_index,
)
from axiom.cli.ext.registry_backend import (
    get as registry_get,
)
from axiom.cli.ext.signing import (
    default_public_key_path,
    load_public_key,
    trusted_keys_dir,
    verify_file,
)

# ---------------------------------------------------------------------------
# Structured view
# ---------------------------------------------------------------------------


@dataclass
class ShowView:
    """What ``show`` prints — decoupled from formatting so tests can drive it."""

    name: str
    version: str
    installed: bool = False
    owner: str = ""
    license: str = ""
    description: str = ""
    published_at: str = ""
    publisher: str = ""
    artifact_sha256: str = ""
    signature_sha256: str = ""
    capabilities: list[str] = field(default_factory=list)
    compatibility: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    signature_status: str = "unknown"  # verified | key unknown | invalid | skipped
    source: str = "registry"  # "registry" | "installed"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "installed": self.installed,
            "owner": self.owner,
            "license": self.license,
            "description": self.description,
            "published_at": self.published_at,
            "publisher": self.publisher,
            "artifact_sha256": self.artifact_sha256,
            "signature_sha256": self.signature_sha256,
            "capabilities": list(self.capabilities),
            "compatibility": dict(self.compatibility),
            "depends_on": list(self.depends_on),
            "signature_status": self.signature_status,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_spec(spec: str) -> tuple[str, str | None]:
    """Split ``name@version`` into ``(name, version_or_none)``."""
    if "@" in spec:
        name, version = spec.split("@", 1)
        name, version = name.strip(), version.strip()
        if not name or not version:
            raise ValueError(
                f"malformed name@version spec: {spec!r}; "
                "expected <name>@<version> with both parts non-empty"
            )
        return name, version
    return spec.strip(), None


def _load_manifest(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def _capabilities_from_manifest(manifest: dict) -> list[str]:
    """Extract ``kind:noun`` strings from ``[[extension.provides]]``."""
    ext = manifest.get("extension", {}) or {}
    raw = ext.get("provides", ()) or ()
    out: list[str] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind", "") or "")
        noun = str(entry.get("noun", "") or "")
        if kind and noun:
            out.append(f"{kind}:{noun}")
        elif kind:
            out.append(kind)
    return out


def _compatibility_from_manifest(manifest: dict) -> dict[str, str]:
    ext = manifest.get("extension", {}) or {}
    compat = ext.get("compatibility", {}) or {}
    if not isinstance(compat, dict):
        return {}
    return {str(k): str(v) for k, v in compat.items()}


def _depends_on_from_manifest(manifest: dict) -> list[str]:
    ext = manifest.get("extension", {}) or {}
    raw = ext.get("depends_on", ()) or ()
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x) for x in raw]


def _check_signature(record: ArtifactRecord) -> str:
    """Verify the artifact signature and return a status label.

    Returns one of ``verified``, ``key unknown``, ``invalid``.
    """
    if not record.sig_path.exists() or not record.artifact_path.exists():
        return "invalid"

    pub_path: Path | None = None
    sha = (record.attestation or {}).get("public_key_sha256")
    if sha:
        candidate = trusted_keys_dir() / f"{sha}.pub"
        if candidate.exists():
            pub_path = candidate
    if pub_path is None:
        default = default_public_key_path()
        if default.exists():
            pub_path = default
    if pub_path is None:
        return "key unknown"

    try:
        pub = load_public_key(pub_path)
    except Exception:
        return "key unknown"

    sig_hex = record.sig_path.read_text(encoding="utf-8").strip()
    ok = verify_file(pub, record.artifact_path, sig_hex)
    return "verified" if ok else "invalid"


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# View builders
# ---------------------------------------------------------------------------


def _resolve_registry_version(name: str, version: str | None) -> str | None:
    if version is not None:
        return version
    idx = read_index()
    entry = (idx.get("extensions") or {}).get(name) or {}
    latest = entry.get("latest")
    return str(latest) if latest else None


def build_registry_view(name: str, version: str | None) -> ShowView | None:
    """Return a :class:`ShowView` for the registry record, or ``None``."""
    resolved = _resolve_registry_version(name, version)
    if resolved is None:
        return None
    record = registry_get(name, resolved)
    if record is None:
        return None

    manifest = _load_manifest(record.manifest_path)
    ext = manifest.get("extension", {}) or {}

    installed_rec = get_installed(name)
    installed_flag = bool(installed_rec and installed_rec.version == resolved)

    att = record.attestation or {}
    artifact_sha = str(att.get("artifact_sha256") or "")
    sig_sha = ""
    if record.sig_path.exists():
        sig_sha = _sha256_of_file(record.sig_path)

    return ShowView(
        name=name,
        version=resolved,
        installed=installed_flag,
        owner=str(ext.get("owner", "") or ""),
        license=str(ext.get("license", "") or ""),
        description=str(ext.get("description", "") or ""),
        published_at=str(att.get("published_at", "") or ""),
        publisher=str(att.get("publisher", "") or ""),
        artifact_sha256=artifact_sha,
        signature_sha256=sig_sha,
        capabilities=_capabilities_from_manifest(manifest),
        compatibility=_compatibility_from_manifest(manifest),
        depends_on=_depends_on_from_manifest(manifest),
        signature_status=_check_signature(record),
        source="registry",
    )


def build_installed_view(name: str) -> ShowView | None:
    """Return a view built from the install-state + the unpacked manifest."""
    rec = get_installed(name)
    if rec is None:
        return None

    view = ShowView(
        name=name,
        version=rec.version,
        installed=True,
        artifact_sha256=rec.artifact_sha256,
        signature_sha256=rec.signature_sha256,
        source="installed",
        signature_status="skipped",
    )

    # If the unpacked directory still has a manifest, fold those fields in.
    install_path = Path(rec.install_path)
    for manifest_candidate in (
        install_path / "axiom-extension.toml",
        install_path / f"{name}-{rec.version}" / "axiom-extension.toml",
    ):
        if manifest_candidate.exists():
            manifest = _load_manifest(manifest_candidate)
            ext = manifest.get("extension", {}) or {}
            view.owner = str(ext.get("owner", "") or "")
            view.license = str(ext.get("license", "") or "")
            view.description = str(ext.get("description", "") or "")
            view.capabilities = _capabilities_from_manifest(manifest)
            view.compatibility = _compatibility_from_manifest(manifest)
            view.depends_on = _depends_on_from_manifest(manifest)
            break

    return view


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _signature_hint(status: str) -> str:
    """One-liner explaining an AEOS signature state in user terms."""
    return {
        "verified": "ed25519 signature checks against a known publisher key",
        "key unknown": "publisher key not in trusted store or self-trust",
        "invalid": "signature does not match the artifact — do not install",
        "skipped": "no on-disk artifact to re-verify",
        "unknown": "state not yet determined",
    }.get(status, "state not yet determined")


def _format_text(view: ShowView) -> str:
    badge = " (installed)" if view.installed else ""
    lines = [f"{view.name} {view.version}{badge}"]
    if view.owner:
        lines.append(f"  owner:          {view.owner}")
    if view.license:
        lines.append(f"  license:        {view.license}")
    if view.description:
        lines.append(f"  description:    {view.description}")
    if view.publisher:
        lines.append(f"  publisher:      {view.publisher}")
    if view.published_at:
        lines.append(f"  published_at:   {view.published_at}")
    if view.artifact_sha256:
        lines.append(f"  artifact SHA:   {view.artifact_sha256}")
    if view.signature_sha256:
        lines.append(f"  signature SHA:  {view.signature_sha256}")
    lines.append(
        f"  signature:      {view.signature_status}"
        f"   ({_signature_hint(view.signature_status)})"
    )
    if view.capabilities:
        lines.append("  capabilities:")
        for cap in view.capabilities:
            lines.append(f"    - {cap}")
    else:
        lines.append("  capabilities:   (none)")
    if view.compatibility:
        lines.append("  compatibility:")
        for k, v in sorted(view.compatibility.items()):
            lines.append(f"    {k}: {v}")
    if view.depends_on:
        lines.append("  depends_on:")
        for dep in view.depends_on:
            lines.append(f"    - {dep}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ShowProvider:
    """Built-in provider for ``axi ext show <name>[@<version>]``."""

    verb = "show"
    description = "Show detailed metadata for a registered or installed extension"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "spec",
            help="Extension name, optionally with @<version>",
        )
        parser.add_argument(
            "--version",
            default=None,
            help="Alternate way to pin a version (e.g. --version 0.1.0)",
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit the view as JSON",
        )
        parser.add_argument(
            "--installed",
            action="store_true",
            help="Read from install-state instead of the registry",
        )
        parser.add_argument(
            "--registry",
            dest="registry_override",
            default=None,
            help="Override the registry URL (file:// only at v0.1)",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        if args.registry_override is not None:
            if not args.registry_override.startswith("file://"):
                print(
                    f"axi ext show: --registry must use the file:// scheme "
                    f"(got {args.registry_override!r})."
                )
                return 1
            os.environ["AXIOM_REGISTRY_URL"] = args.registry_override

        try:
            name, spec_version = parse_spec(args.spec)
        except ValueError as exc:
            print(f"axi ext show: {exc}")
            return 2

        version = args.version or spec_version

        if args.installed:
            view = build_installed_view(name)
            if view is None:
                print(
                    f"axi ext show: {name} is not axi-installed; "
                    "run `axi ext list` to see installed extensions, or "
                    "drop --installed to query the registry."
                )
                return 1
            # If a version was pinned and doesn't match, that's a mismatch.
            if version is not None and view.version != version:
                print(
                    f"axi ext show: {name} is installed at "
                    f"{view.version!r}, not {version!r}."
                )
                return 1
        else:
            view = build_registry_view(name, version)
            if view is None:
                where = f"{name}@{version}" if version else name
                print(
                    f"axi ext show: {where} not found in the registry; "
                    "run `axi ext search <query>` to find extensions."
                )
                return 1

        con = console()
        if args.as_json:
            con.print(json.dumps(view.to_json(), indent=2, sort_keys=True))
            return 0

        con.print(_format_text(view))
        if not view.installed and view.source == "registry":
            con.print("")
            next_steps(
                [
                    f"axi ext install {view.name}    # Install the latest",
                ]
            )
        return 0


__all__ = [
    "ShowProvider",
    "ShowView",
    "build_installed_view",
    "build_registry_view",
    "parse_spec",
]
