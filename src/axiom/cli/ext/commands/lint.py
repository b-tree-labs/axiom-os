# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext lint`` — Bronze-level AEOS conformance check.

See :doc:`spec-aeos-0.1 §12.1` for the Bronze definition. This implementation
is intentionally conservative: every check emits a structured report entry
with a remediation hint, so the output doubles as a guided-fix checklist.
"""

from __future__ import annotations

import argparse
import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from axiom.cli.ext._output import console, next_steps, status
from axiom.cli.ext.provider import CliContext

# ---------------------------------------------------------------------------
# Finding model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """A single lint finding.

    ``severity`` is ``error``, ``warning``, or ``info``. Errors cause a
    non-zero exit; warnings are reported but non-fatal.
    """

    code: str
    severity: str
    message: str
    remediation: str


def _error(code: str, message: str, remediation: str) -> Finding:
    return Finding(code=code, severity="error", message=message, remediation=remediation)


def _warn(code: str, message: str, remediation: str) -> Finding:
    return Finding(code=code, severity="warning", message=message, remediation=remediation)


# ---------------------------------------------------------------------------
# Lint rules
# ---------------------------------------------------------------------------


# Required files per AEOS §5.2
_REQUIRED_FILES: tuple[str, ...] = (
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "pyproject.toml",
    "axiom-extension.toml",
)

# Relaxed file set for built-ins (manifest sets builtin = true). README,
# CHANGELOG, LICENSE, pyproject belong to the host package.
_BUILTIN_REQUIRED_FILES: tuple[str, ...] = (
    "axiom-extension.toml",
)

# Compound-layout subdirectories per AEOS §5.1
_CAPABILITY_DIRS: tuple[str, ...] = (
    "agents",
    "tools",
    "commands",
    "services",
    "adapters",
    "skills",
    "hooks",
)


def lint_extension(ext_path: Path) -> list[Finding]:
    """Run Bronze conformance checks against ``ext_path``. Return findings."""
    findings: list[Finding] = []

    if not ext_path.exists() or not ext_path.is_dir():
        return [
            _error(
                code="AEOS001",
                message=f"not a directory: {ext_path}",
                remediation="point `axi ext lint` at the extension's root directory",
            )
        ]

    # -- manifest: load first so we know whether this is a built-in ------
    manifest_path = ext_path / "axiom-extension.toml"
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = _load_manifest(manifest_path)
        except Exception as exc:
            findings.append(
                _error(
                    code="AEOS020",
                    message=f"could not parse axiom-extension.toml: {exc}",
                    remediation="fix TOML syntax; run `python -c 'import tomllib; tomllib.load(open(\"axiom-extension.toml\", \"rb\"))'`",
                )
            )
            return findings

    is_builtin = bool(manifest.get("extension", {}).get("builtin", False))

    # -- required top-level files ------------------------------------------
    required = _BUILTIN_REQUIRED_FILES if is_builtin else _REQUIRED_FILES
    for name in required:
        if not (ext_path / name).exists():
            findings.append(
                _error(
                    code="AEOS010",
                    message=f"missing required file: {name}",
                    remediation=(
                        f"create {name} at the extension root; "
                        "see AEOS §5.2 for required files"
                    ),
                )
            )

    # Bail if the manifest is missing — later checks depend on it.
    if not manifest_path.exists():
        return findings

    schema_errors = _validate_schema(manifest)
    for err in schema_errors:
        findings.append(
            _error(
                code="AEOS021",
                message=f"manifest schema violation: {err}",
                remediation="see docs/specs/spec-aeos-0.1.md §6 for the required schema",
            )
        )

    ext_block = manifest.get("extension", {})
    declared_name = ext_block.get("name", "")
    aeos_version = ext_block.get("aeos_version")
    if not aeos_version:
        findings.append(
            _error(
                code="AEOS022",
                message="manifest [extension] missing aeos_version",
                remediation='add `aeos_version = "0.1.0"` to [extension] (AEOS §6.2)',
            )
        )

    # -- name consistency: dir ↔ package ↔ manifest ↔ pyproject ----------
    dir_name = ext_path.name
    if declared_name and declared_name != dir_name:
        findings.append(
            _error(
                code="AEOS030",
                message=(
                    f"manifest name {declared_name!r} does not match directory "
                    f"name {dir_name!r}"
                ),
                remediation="align the two or rename the directory; AEOS §5.4 requires they match",
            )
        )

    # Compound layout: <ext>/<pkg>/__init__.py
    # Flat-builtin layout: <ext>/__init__.py, when the extension's
    # manifest sets builtin = true and the ext directory name matches
    # the declared package name.
    is_builtin = bool(ext_block.get("builtin", False))
    pkg_dir_compound = ext_path / (declared_name or dir_name)
    pkg_init_flat = ext_path / "__init__.py"

    if (pkg_dir_compound / "__init__.py").exists():
        init_path = pkg_dir_compound / "__init__.py"
    elif is_builtin and pkg_init_flat.exists():
        init_path = pkg_init_flat  # flat built-in layout
    else:
        init_path = None
        findings.append(
            _error(
                code="AEOS031",
                message=(
                    f"missing Python package {declared_name or dir_name!r} "
                    "next to the manifest"
                ),
                remediation=(
                    f"create {declared_name or dir_name}/__init__.py (AEOS §5.1) "
                    "or set `builtin = true` + place __init__.py at the ext root "
                    "for flat built-in layout"
                ),
            )
        )

    if init_path is not None and init_path != pkg_init_flat:
        # __all__ only enforced for non-flat layouts; built-ins inherit
        # the host package's public API.
        init_text = init_path.read_text(encoding="utf-8")
        if not _declares_all(init_text):
            findings.append(
                _error(
                    code="AEOS032",
                    message=f"{init_path.parent.name}/__init__.py does not declare __all__",
                    remediation="add `__all__: list[str] = []` (AEOS §7.3)",
                )
            )

    # pyproject alignment
    pyproj_path = ext_path / "pyproject.toml"
    if pyproj_path.exists():
        try:
            pyproject = _load_manifest(pyproj_path)
        except Exception as exc:  # noqa: BLE001
            findings.append(
                _error(
                    code="AEOS035",
                    message=f"could not parse pyproject.toml: {exc}",
                    remediation="fix TOML syntax",
                )
            )
        else:
            py_name = pyproject.get("project", {}).get("name", "")
            if py_name and declared_name:
                norm_py = py_name.replace("-", "_")
                norm_m = declared_name.replace("-", "_")
                # Allow exact match OR host-package-prefixed form (e.g.
                # manifest "diagnostics" ↔ pyproject "axiom-diagnostics").
                aligned = norm_py == norm_m or norm_py.endswith("_" + norm_m)
                if not aligned:
                    findings.append(
                        _error(
                            code="AEOS036",
                            message=(
                                f"pyproject name {py_name!r} does not match "
                                f"manifest name {declared_name!r}"
                            ),
                            remediation=(
                                "align [project].name with [extension].name "
                                "(exact or host-prefixed like 'axiom-<name>')"
                            ),
                        )
                    )
            py_version = pyproject.get("project", {}).get("version")
            manifest_version = ext_block.get("version")
            if py_version and manifest_version and py_version != manifest_version:
                findings.append(
                    _error(
                        code="AEOS037",
                        message=(
                            f"pyproject version {py_version!r} does not match "
                            f"manifest version {manifest_version!r}"
                        ),
                        remediation="align [project].version with [extension].version",
                    )
                )

    # -- layout: capability-kind subdirs ----------------------------------
    # For flat-builtin layouts, the capability-kind subdirs live at the
    # ext root itself; for compound layouts they live inside pkg_dir.
    layout_root = ext_path if (is_builtin and init_path == pkg_init_flat) else pkg_dir_compound
    for kind in _CAPABILITY_DIRS:
        if not (layout_root / kind).is_dir():
            findings.append(
                _warn(
                    code="AEOS040",
                    message=f"compound-layout subdirectory missing: {kind}/",
                    remediation=(
                        f"create {layout_root.name}/{kind}/ (compound-by-default per AEOS §3.3); "
                        "the scaffold from `axi ext init` includes it"
                    ),
                )
            )

    # -- standard test file ------------------------------------------------
    std_test = ext_path / "tests" / "unit_tests" / "test_standard.py"
    if not std_test.exists():
        findings.append(
            _error(
                code="AEOS050",
                message="missing tests/unit_tests/test_standard.py",
                remediation=(
                    "create a test inheriting from `axiom_tests.unit_tests.ExtensionStandardTests` "
                    "(AEOS §8.2)"
                ),
            )
        )

    # -- persona + skill hints --------------------------------------------
    for provided in ext_block.get("provides", []) or []:
        kind = provided.get("kind")
        if kind == "agent" and provided.get("persona"):
            persona_path = ext_path / provided["persona"]
            if not persona_path.exists():
                findings.append(
                    _error(
                        code="AEOS060",
                        message=f"agent persona not found: {provided['persona']}",
                        remediation=(
                            f"create {provided['persona']} next to the agent module; "
                            "persona.md is the agent's own system prompt (AEOS §4.1)"
                        ),
                    )
                )
        if kind == "skill" and provided.get("path"):
            skill_md = ext_path / provided["path"] / "SKILL.md"
            if not skill_md.exists():
                findings.append(
                    _warn(
                        code="AEOS061",
                        message=f"skill declared but SKILL.md missing at {skill_md}",
                        remediation="add a SKILL.md with agentskills.io frontmatter (AEOS §4.6)",
                    )
                )

    # -- [extension.mcp] block (spec-builtin-mcp-server.md §6.3) ----------
    findings.extend(_mcp_block_findings(manifest_path))

    return findings


def _mcp_block_findings(manifest_path: Path) -> list[Finding]:
    """Lift ``lint_mcp_block`` findings into the ``Finding`` shape.

    Maps the MCP-block lint output to AEOS-coded findings so they appear
    in the same lint report and respect the same exit-code rule. Codes:

    - ``AEOS070`` — neither block nor opt-out comment.
    - ``AEOS071`` — extension tool name collides with a platform primitive.
    - ``AEOS072`` — malformed Matrix-style principal in allowed_principals.
    - ``AEOS073`` — other lint findings (visibility/auth/prefix/principals).
    """
    from axiom.extensions.builtins.mcp.manifest_schema import (
        LintError,
        LintWarning,
        lint_mcp_block,
    )

    out: list[Finding] = []
    try:
        raw = lint_mcp_block(manifest_path)
    except FileNotFoundError:
        return out
    except Exception as exc:  # noqa: BLE001 — defensive: never crash lint
        out.append(
            _warn(
                code="AEOS073",
                message=f"could not lint [extension.mcp] block: {exc}",
                remediation=(
                    "open the manifest and verify the [extension.mcp] section "
                    "matches spec-builtin-mcp-server.md §7"
                ),
            )
        )
        return out

    for finding in raw:
        message = finding.message
        # Code routing: keep the spec-aligned subset stable for tests
        # and downstream consumers; everything else becomes AEOS073.
        if "no [extension.mcp] block" in message:
            code = "AEOS070"
            remediation = (
                "add `[extension.mcp]\\nenabled = true` (or `enabled = false` to opt out) "
                "to the manifest, OR add a one-line comment above [extension] of the form "
                "`# mcp: not-applicable -- <reason>`. See spec-builtin-mcp-server.md §6.3."
            )
        elif "platform-primitive tool name" in message:
            code = "AEOS071"
            remediation = (
                "rename the colliding mcp_name (or remove the override and rely on the "
                "default `axiom_<extension>__<tool>` prefix). Platform tool names always win."
            )
        elif "malformed Matrix-style identity" in message:
            code = "AEOS072"
            remediation = (
                "use the `@name:context` Matrix-style form for allowed_principals "
                "(e.g., `@*:local`, `@alice:axiom.example.org`). See spec §7.2."
            )
        else:
            code = "AEOS073"
            remediation = (
                "see spec-builtin-mcp-server.md §7 for valid [extension.mcp] schema"
            )

        if isinstance(finding, LintError):
            out.append(_error(code=code, message=message, remediation=remediation))
        elif isinstance(finding, LintWarning):
            out.append(_warn(code=code, message=message, remediation=remediation))
        else:  # defensive
            out.append(_warn(code=code, message=str(finding), remediation=remediation))

    return out


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _declares_all(source: str) -> bool:
    """Return True iff ``source`` contains an ``__all__`` assignment.

    Uses AST parsing so a comment mentioning ``__all__`` does not falsely
    satisfy the check.
    """
    import ast as _ast

    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return False
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Assign):
            for target in node.targets:
                if isinstance(target, _ast.Name) and target.id == "__all__":
                    return True
        elif isinstance(node, _ast.AnnAssign):
            if isinstance(node.target, _ast.Name) and node.target.id == "__all__":
                return True
    return False


def _validate_schema(manifest: dict[str, Any]) -> list[str]:
    """Validate against the AEOS JSON Schema shipped with ``axiom-tests``.

    Import is local — `axi ext lint` is the only caller, and we avoid paying
    the import cost during generic CLI startup.
    """
    try:
        from axiom_tests import validate_manifest
    except ImportError as exc:  # pragma: no cover — hard dependency for lint
        return [f"axiom-tests not installed: {exc}"]
    return validate_manifest(manifest)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LintProvider:
    """Built-in provider for ``axi ext lint [<path>]``.

    Bronze is the first rung of the AEOS conformance ladder (spec §12.1): it
    certifies that the extension has a valid directory layout and a parseable
    ``axiom-extension.toml`` manifest. Silver adds signed releases + passing
    standard tests; Gold adds behavioral classification attestations.
    """

    verb = "lint"
    description = (
        "Verify Bronze-level AEOS conformance (layout + manifest; AEOS §12.1)"
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Path to the extension (default: current working directory)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit findings as JSON",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        target = Path(args.path).resolve() if args.path else context.cwd
        findings = lint_extension(target)
        errors = [f for f in findings if f.severity == "error"]

        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "extension": str(target),
                        "findings": [asdict(f) for f in findings],
                        "error_count": len(errors),
                    },
                    indent=2,
                )
            )
            return 1 if errors else 0

        con = console()
        if not findings:
            con.print(
                f"axi ext lint: {target.name}: "
                "OK (Bronze — layout + manifest ok)"
            )
            con.print("")
            next_steps(
                [
                    "axi ext test                 # Run the standard tests",
                    "axi ext scan                 # Pre-publish policy gate",
                ]
            )
            return 0

        con.print(f"axi ext lint: {target.name}: {len(findings)} finding(s)")
        con.print("")
        for f in findings:
            level = "fail" if f.severity == "error" else "warn"
            status(level, f.code, f.message)
            con.print(f"          → {f.remediation}")
            con.print("")
        return 1 if errors else 0


__all__ = ["Finding", "LintProvider", "lint_extension"]
