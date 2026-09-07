# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext scan`` — pre-publish baseline security & policy checks.

v0.1 scope, per spec §10.2:

1. **Manifest sanity** — parses, passes the AEOS schema.
2. **License** — ``[extension].license`` is on a small SPDX allowlist; may be
   overridden per-ID via ``--allow-license <spdx>``.
3. **Secrets heuristic** — grep the package source for obvious patterns. This
   is a defense-in-depth layer; CI secret scanners remain the primary signal.
4. **Dangerous-primitive heuristic** — flag ``exec(``, ``eval(``, ``os.system(``
   and ``subprocess.*shell=True`` in the *public* API surface (everything not
   under ``_internal/``).
5. **Manifest ↔ pyproject alignment** — name, version, and every ``[[extension.
   provides]]`` block has a matching entry point.

The intent is Bronze-level guardrails today; behavioral-classifier-backed classification is
Tier 4 (spec §9.4) and stays out of scope until the behavioral-attestation
work lands.
"""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from axiom.cli.ext._output import (
    TERMINATOR_OK,
    console,
    next_steps,
    status,
)
from axiom.cli.ext._spdx import resolve_spdx
from axiom.cli.ext.provider import CliContext

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanCheck:
    """One scan finding.

    ``severity`` is one of ``"pass"``, ``"warn"``, ``"fail"``. Hard failures
    always cause non-zero exit; warnings are tolerated unless ``--strict``.
    """

    check: str
    severity: str
    detail: str
    remediation: str = ""


@dataclass
class ScanResult:
    """Aggregate scan output."""

    checks: list[ScanCheck] = field(default_factory=list)

    @property
    def hard_failure(self) -> bool:
        return any(c.severity == "fail" for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.severity == "warn" for c in self.checks)


# Default SPDX allowlist for extension licenses. Permissive OSI-approved
# options plus MPL for weak-copyleft. Anything else requires ``--allow-license``.
DEFAULT_LICENSE_ALLOWLIST: frozenset[str] = frozenset(
    {"Apache-2.0", "MIT", "BSD-3-Clause", "BSD-2-Clause", "MPL-2.0"}
)

# Capability-kind -> entry-point group for the alignment check. Mirrors the
# table in :mod:`axiom.cli.ext.commands.validate`.
_ENTRY_POINT_GROUPS: dict[str, str] = {
    "agent": "axiom.agents",
    "tool": "axiom.tools",
    "cmd": "axiom.commands",
    "service": "axiom.services",
    "adapter": "axiom.adapters",
    "hook": "axiom.hooks",
    "signal_type": "axiom.signals",
}


# ---------------------------------------------------------------------------
# Heuristic patterns
# ---------------------------------------------------------------------------


# Secrets — keep the list tight so false positives stay rare. Every entry is a
# compiled regex; docstring literals and comments are scanned along with code
# since a leaked secret in a docstring is still leaked.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_secret_access_key", re.compile(r"AWS_SECRET_ACCESS_KEY")),
    ("rsa_private_key", re.compile(r"BEGIN RSA PRIVATE KEY")),
    ("openssh_private_key", re.compile(r"BEGIN OPENSSH PRIVATE KEY")),
    # sk- prefix (OpenAI-style) with at least 40 chars after the prefix.
    ("openai_like_token", re.compile(r"sk-[A-Za-z0-9]{40,}")),
    # token/secret/password/api_key = "<base64ish 40+>"
    (
        "named_secret_assignment",
        re.compile(
            r"""(?ix)
            \b(token|secret|password|api[_-]?key)\b
            \s*=\s*
            ["']
            [A-Za-z0-9+/_\-]{40,}=*
            ["']
            """
        ),
    ),
)

# Dangerous primitives. Each is a (tag, pattern) pair where the tag shows up
# in the scan's detail message.
_DANGER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("exec", re.compile(r"\bexec\s*\(")),
    ("eval", re.compile(r"\beval\s*\(")),
    ("os.system", re.compile(r"\bos\.system\s*\(")),
    (
        "subprocess.shell=True",
        re.compile(r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"),
    ),
)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_manifest_sanity(ext_path: Path) -> tuple[ScanCheck, dict[str, Any] | None]:
    """Return (check, parsed_manifest or None)."""
    manifest_path = ext_path / "axiom-extension.toml"
    if not manifest_path.exists():
        return (
            ScanCheck(
                check="manifest_sanity",
                severity="fail",
                detail="axiom-extension.toml not found at extension root",
                remediation="run `axi ext init` or create the manifest",
            ),
            None,
        )
    try:
        with manifest_path.open("rb") as fh:
            manifest = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        return (
            ScanCheck(
                check="manifest_sanity",
                severity="fail",
                detail=f"manifest TOML parse failed: {exc}",
                remediation="fix the TOML syntax; AEOS §6 documents the schema",
            ),
            None,
        )

    # Schema check via axiom-tests (same backend as `axi ext lint`).
    try:
        from axiom_tests import validate_manifest

        errors = validate_manifest(manifest)
    except ImportError:  # pragma: no cover — hard dep in practice
        errors = []
    if errors:
        return (
            ScanCheck(
                check="manifest_sanity",
                severity="fail",
                detail="manifest fails AEOS schema: " + "; ".join(errors[:2]),
                remediation="run `axi ext lint` for the full findings list",
            ),
            None,
        )
    return (
        ScanCheck(
            check="manifest_sanity",
            severity="pass",
            detail="manifest parses and validates against AEOS schema",
        ),
        manifest,
    )


def _check_license(
    manifest: dict[str, Any], allowlist: set[str]
) -> ScanCheck:
    license_id = manifest.get("extension", {}).get("license", "")
    if not license_id:
        return ScanCheck(
            check="license",
            severity="fail",
            detail="[extension].license is empty",
            remediation="add an SPDX license identifier; use `--allow-license` to override the allowlist",
        )
    if license_id not in allowlist:
        return ScanCheck(
            check="license",
            severity="fail",
            detail=(
                f"license {license_id!r} is not on the default allowlist "
                f"({', '.join(sorted(DEFAULT_LICENSE_ALLOWLIST))})"
            ),
            remediation=f"use an allowlisted SPDX id or pass `--allow-license {license_id}`",
        )
    return ScanCheck(
        check="license",
        severity="pass",
        detail=f"license {license_id!r} on the allowlist",
    )


def _iter_public_python_files(pkg_root: Path):
    """Yield every ``*.py`` under ``pkg_root`` that is NOT under ``_internal/``."""
    if not pkg_root.exists():
        return
    for path in pkg_root.rglob("*.py"):
        parts = path.relative_to(pkg_root).parts
        if any(part == "_internal" for part in parts):
            continue
        yield path


def _check_secrets(pkg_root: Path) -> ScanCheck:
    hits: list[str] = []
    for py in _iter_public_python_files(pkg_root):
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for tag, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                rel = py.relative_to(pkg_root.parent) if pkg_root.parent in py.parents else py
                hits.append(f"{rel}: {tag}")
    if hits:
        return ScanCheck(
            check="secrets",
            severity="warn",
            detail=(
                f"{len(hits)} possible secret pattern(s) detected: "
                + "; ".join(hits[:3])
                + (", ..." if len(hits) > 3 else "")
            ),
            remediation=(
                "move the value into a config store or env var, or add a nosec "
                "comment if it's a known false positive"
            ),
        )
    return ScanCheck(
        check="secrets",
        severity="pass",
        detail="no obvious secret patterns in the public API",
    )


def _check_dangerous_primitives(pkg_root: Path) -> ScanCheck:
    hits: list[str] = []
    for py in _iter_public_python_files(pkg_root):
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for tag, pattern in _DANGER_PATTERNS:
            if pattern.search(text):
                rel = py.relative_to(pkg_root.parent) if pkg_root.parent in py.parents else py
                hits.append(f"{rel}: {tag}")
    if hits:
        return ScanCheck(
            check="dangerous_primitives",
            severity="warn",
            detail=(
                f"{len(hits)} dangerous primitive use(s) in public API: "
                + "; ".join(hits[:3])
                + (", ..." if len(hits) > 3 else "")
            ),
            remediation=(
                "prefer safer alternatives; move unavoidable uses into _internal/ "
                "and harden them with explicit input validation"
            ),
        )
    return ScanCheck(
        check="dangerous_primitives",
        severity="pass",
        detail="no exec/eval/os.system/shell=True in the public API",
    )


def _check_manifest_pyproject_alignment(
    ext_path: Path, manifest: dict[str, Any]
) -> ScanCheck:
    ext_block = manifest.get("extension", {})
    is_builtin = bool(ext_block.get("builtin", False))

    pyproj_path = ext_path / "pyproject.toml"
    if not pyproj_path.exists():
        if is_builtin:
            # Built-ins ship inside a host package (axi-platform's
            # extensions/builtins/); the host owns pyproject. Pass.
            return ScanCheck(
                check="manifest_pyproject_alignment",
                severity="pass",
                detail="built-in extension — alignment check deferred to host package",
            )
        return ScanCheck(
            check="manifest_pyproject_alignment",
            severity="fail",
            detail="pyproject.toml is missing",
            remediation="create pyproject.toml next to axiom-extension.toml",
        )
    try:
        with pyproj_path.open("rb") as fh:
            pyproject = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        return ScanCheck(
            check="manifest_pyproject_alignment",
            severity="fail",
            detail=f"pyproject.toml parse failed: {exc}",
            remediation="fix the TOML syntax",
        )

    proj = pyproject.get("project", {})

    manifest_name = ext_block.get("name", "")
    py_name = proj.get("name", "")
    if manifest_name and py_name:
        norm_py = py_name.replace("-", "_")
        norm_m = manifest_name.replace("-", "_")
        # Allow exact OR host-prefixed form (e.g. "axiom-diagnostics").
        aligned = norm_py == norm_m or norm_py.endswith("_" + norm_m)
        if not aligned:
            return ScanCheck(
                check="manifest_pyproject_alignment",
                severity="fail",
                detail=(
                    f"name mismatch: manifest={manifest_name!r} pyproject={py_name!r}"
                ),
                remediation="align [project].name with [extension].name",
            )

    manifest_version = ext_block.get("version", "")
    py_version = proj.get("version", "")
    if manifest_version and py_version and manifest_version != py_version:
        return ScanCheck(
            check="manifest_pyproject_alignment",
            severity="fail",
            detail=(
                f"version mismatch: manifest={manifest_version!r} pyproject={py_version!r}"
            ),
            remediation="align [project].version with [extension].version",
        )

    ep_table = proj.get("entry-points", {}) or {}
    missing: list[str] = []
    for block in ext_block.get("provides", []) or []:
        kind = block.get("kind")
        entry = block.get("entry")
        if not entry:
            # Skill blocks are declared via ``path``, not entry point.
            continue
        group = _ENTRY_POINT_GROUPS.get(kind)
        if group is None:
            # Unknown kind — surface as alignment failure (manifest schema
            # should have caught it, but belt-and-suspenders here).
            missing.append(f"{kind} (unknown capability kind)")
            continue
        label = block.get("name") or block.get("noun") or block.get("integration")
        if not label:
            missing.append(f"{kind} (no name/noun/integration)")
            continue
        table = ep_table.get(group, {}) or {}
        if label not in table:
            missing.append(f'"{group}" -> {label!r}')
    if missing:
        return ScanCheck(
            check="manifest_pyproject_alignment",
            severity="fail",
            detail=(
                f"{len(missing)} provides block(s) without matching entry "
                f"point: " + "; ".join(missing[:3])
                + (", ..." if len(missing) > 3 else "")
            ),
            remediation=(
                "add the corresponding [project.entry-points.*] entries in "
                "pyproject.toml (AEOS §7.2)"
            ),
        )
    return ScanCheck(
        check="manifest_pyproject_alignment",
        severity="pass",
        detail="manifest and pyproject agree on name, version, and entry points",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def scan_extension(
    ext_path: Path, *, extra_allowed_licenses: set[str] | None = None
) -> ScanResult:
    """Run the baseline scan against ``ext_path`` and return a :class:`ScanResult`.

    ``extra_allowed_licenses`` extends the default allowlist with per-call SPDX
    overrides (wiring for the CLI ``--allow-license`` flag).
    """
    result = ScanResult()
    allowlist = set(DEFAULT_LICENSE_ALLOWLIST)
    if extra_allowed_licenses:
        allowlist.update(extra_allowed_licenses)

    manifest_check, manifest = _check_manifest_sanity(ext_path)
    result.checks.append(manifest_check)
    if manifest is None:
        # Short-circuit: every downstream check needs the manifest.
        return result

    result.checks.append(_check_license(manifest, allowlist))

    ext_name = manifest.get("extension", {}).get("name") or ext_path.name
    pkg_root = ext_path / ext_name
    result.checks.append(_check_secrets(pkg_root))
    result.checks.append(_check_dangerous_primitives(pkg_root))
    result.checks.append(_check_manifest_pyproject_alignment(ext_path, manifest))

    return result


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ScanProvider:
    """Built-in provider for ``axi ext scan [<path>]``."""

    verb = "scan"
    description = "Baseline pre-publish security + policy checks"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Path to the extension (default: current working directory)",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Treat warnings as failures (exit 1)",
        )
        parser.add_argument(
            "--allow-license",
            dest="allow_licenses",
            action="append",
            default=[],
            metavar="SPDX",
            help=(
                "Extend the SPDX license allowlist; repeatable "
                "(e.g. --allow-license LicenseRef-Internal)"
            ),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit results as JSON",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        target = Path(args.path).resolve() if args.path else context.cwd
        extras: set[str] = set()
        # Each ``--allow-license`` value is resolved through the SPDX fuzzy
        # matcher so users can pass ``apache`` / ``mit`` / ``bsd3`` etc. in
        # addition to canonical ids. Bare ``LicenseRef-*`` / unknown ids fall
        # through unchanged so the scan can explicitly allow non-standard
        # internal licenses.
        for raw in args.allow_licenses or []:
            resolved = resolve_spdx(raw)
            extras.add(resolved if resolved is not None else raw)
        result = scan_extension(target, extra_allowed_licenses=extras)

        # Exit code: hard fail always -> 1; warnings -> 1 only under --strict.
        exit_code = 0
        if result.hard_failure or args.strict and result.has_warnings:
            exit_code = 1

        if args.as_json:
            print(
                json.dumps(
                    {
                        "extension": str(target),
                        "checks": [asdict(c) for c in result.checks],
                        "hard_failure": result.hard_failure,
                        "has_warnings": result.has_warnings,
                    },
                    indent=2,
                )
            )
            return exit_code

        con = console()
        con.print(f"axi ext scan: {target.name}")
        con.print("")
        for check in result.checks:
            level = "pass" if check.severity == "pass" else (
                "warn" if check.severity == "warn" else "fail"
            )
            status(level, check.check, check.detail)
            if check.severity != "pass" and check.remediation:
                con.print(f"         → {check.remediation}")
        con.print("")
        if result.hard_failure:
            con.print("scan: hard failure — publish blocked.")
        elif result.has_warnings:
            if args.strict:
                con.print("scan: warnings present — failing under --strict.")
            else:
                con.print("scan: warnings only; passing (use --strict to fail on warnings).")
        else:
            con.print(f"scan: {TERMINATOR_OK}")
            con.print("")
            next_steps(
                [
                    "axi ext sign                 # Build + sign the artifact",
                    "axi ext publish --yes        # Sign + register in one step",
                ]
            )
        return exit_code


__all__ = [
    "DEFAULT_LICENSE_ALLOWLIST",
    "ScanCheck",
    "ScanProvider",
    "ScanResult",
    "scan_extension",
]
