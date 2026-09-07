# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext validate`` — deeper AEOS conformance checks.

Validate is the next level up from :mod:`axi ext lint`. Where ``lint`` checks
layout, ``validate`` actually loads the extension and verifies behaviour:

- Every ``[[extension.provides]]`` entry resolves to a real Python entry
  point declared in ``pyproject.toml``.
- The standard test file runs (``pytest tests/unit_tests/test_standard.py``).
- Every symbol in ``__all__`` is importable at runtime.
- A grep-based scan for forbidden private imports flags obvious offenders
  (full import-linter integration is a later agent — see TODO below).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from axiom.cli.ext._output import console, next_steps, status
from axiom.cli.ext.provider import CliContext

# Entry-point group per AEOS §7.2 — capability ``kind`` maps to the pyproject
# ``project.entry-points.<group>`` name.
_ENTRY_POINT_GROUPS: dict[str, str] = {
    "agent": "axiom.agents",
    "tool": "axiom.tools",
    "cmd": "axiom.commands",
    "service": "axiom.services",
    "adapter": "axiom.adapters",
    "hook": "axiom.hooks",
    "signal_type": "axiom.signals",
}


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a single validation check."""

    check: str
    ok: bool
    detail: str
    remediation: str = ""


def validate_extension(ext_path: Path) -> list[ValidationResult]:
    """Run validate checks against ``ext_path``; return per-check results."""
    results: list[ValidationResult] = []

    manifest_path = ext_path / "axiom-extension.toml"
    pyproj_path = ext_path / "pyproject.toml"

    if not manifest_path.exists() or not pyproj_path.exists():
        results.append(
            ValidationResult(
                check="preflight",
                ok=False,
                detail="axiom-extension.toml or pyproject.toml missing",
                remediation="run `axi ext lint` first and resolve file-presence errors",
            )
        )
        return results

    with manifest_path.open("rb") as fh:
        manifest = tomllib.load(fh)
    with pyproj_path.open("rb") as fh:
        pyproject = tomllib.load(fh)

    results.extend(_check_entry_points(manifest, pyproject))
    results.extend(_check_public_api(ext_path, manifest))
    results.extend(_check_forbidden_imports(ext_path, manifest))

    return results


def _check_entry_points(
    manifest: dict[str, Any], pyproject: dict[str, Any]
) -> list[ValidationResult]:
    """Each provides-block with an `entry` field must resolve to a pyproject entry point."""
    results: list[ValidationResult] = []
    ep_table = (
        pyproject.get("project", {})
        .get("entry-points", {})
    )

    for block in manifest.get("extension", {}).get("provides", []) or []:
        kind = block.get("kind")
        entry = block.get("entry")
        if not entry:
            # Skill blocks use `path`, not `entry` — nothing to check.
            continue
        group = _ENTRY_POINT_GROUPS.get(kind)
        if group is None:
            results.append(
                ValidationResult(
                    check=f"entry_point[{kind}]",
                    ok=False,
                    detail=f"unknown capability kind {kind!r}",
                    remediation="use one of agent, tool, cmd, service, adapter, skill, hook, signal_type",
                )
            )
            continue
        table = ep_table.get(group, {})
        label = block.get("name") or block.get("noun") or block.get("integration")
        if not label:
            results.append(
                ValidationResult(
                    check=f"entry_point[{kind}]",
                    ok=False,
                    detail=f"{kind} block missing name/noun/integration",
                    remediation="add the identifying field required by the schema",
                )
            )
            continue
        if label not in table:
            results.append(
                ValidationResult(
                    check=f"entry_point[{kind}:{label}]",
                    ok=False,
                    detail=(
                        f"manifest declares {kind} {label!r} but pyproject has no entry "
                        f'point "{group}" → "{label}"'
                    ),
                    remediation=(
                        f"add `[project.entry-points.\"{group}\"]\\n{label} = \"{entry}\"` "
                        "to pyproject.toml"
                    ),
                )
            )
            continue
        if table[label] != entry:
            results.append(
                ValidationResult(
                    check=f"entry_point[{kind}:{label}]",
                    ok=False,
                    detail=(
                        f"manifest entry {entry!r} does not match pyproject "
                        f"entry point {table[label]!r}"
                    ),
                    remediation="align the two to the same `module:symbol` target",
                )
            )
        else:
            results.append(
                ValidationResult(
                    check=f"entry_point[{kind}:{label}]",
                    ok=True,
                    detail=f"{entry} resolves via {group}",
                )
            )

    return results


def _check_public_api(ext_path: Path, manifest: dict[str, Any]) -> list[ValidationResult]:
    """Every symbol declared in ``__all__`` must be importable from the package."""
    pkg_name = manifest.get("extension", {}).get("name") or ext_path.name
    init_path = ext_path / pkg_name / "__init__.py"
    if not init_path.exists():
        return [
            ValidationResult(
                check="public_api",
                ok=False,
                detail=f"{pkg_name}/__init__.py not found",
                remediation="create the package __init__.py with `__all__: list[str] = []`",
            )
        ]

    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [
            ValidationResult(
                check="public_api",
                ok=False,
                detail=f"syntax error in {init_path}: {exc}",
                remediation="fix the syntax error",
            )
        ]

    names = _extract_all(tree)
    if names is None:
        return [
            ValidationResult(
                check="public_api",
                ok=False,
                detail=f"{init_path} does not define __all__ as a list/tuple literal",
                remediation="declare `__all__: list[str] = [...]` with a literal list",
            )
        ]

    # Empty __all__ is fine for a fresh scaffold — nothing to verify.
    if not names:
        return [
            ValidationResult(
                check="public_api",
                ok=True,
                detail="__all__ is empty (fresh scaffold); nothing to import",
            )
        ]

    results: list[ValidationResult] = []
    # Run the import in a subprocess so a broken extension does not poison our
    # own Python state, and so PYTHONPATH can be adjusted cleanly.
    code = (
        "import importlib, sys; "
        f"sys.path.insert(0, {str(ext_path)!r}); "
        f"mod = importlib.import_module({pkg_name!r}); "
        "missing = [n for n in " + repr(names) + " if not hasattr(mod, n)]; "
        "print('MISSING=' + ','.join(missing))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return [
            ValidationResult(
                check="public_api",
                ok=False,
                detail=f"could not import package {pkg_name!r}: {proc.stderr.strip()}",
                remediation=f"make {pkg_name}/__init__.py importable",
            )
        ]
    missing_line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("MISSING=")),
        "MISSING=",
    )
    missing = [n for n in missing_line[len("MISSING="):].split(",") if n]
    if missing:
        results.append(
            ValidationResult(
                check="public_api",
                ok=False,
                detail=f"symbols in __all__ missing at runtime: {missing}",
                remediation=(
                    "import the symbols at the top of __init__.py before listing them "
                    "in __all__"
                ),
            )
        )
    else:
        results.append(
            ValidationResult(
                check="public_api",
                ok=True,
                detail=f"all {len(names)} symbol(s) in __all__ import cleanly",
            )
        )
    return results


def _extract_all(tree: ast.AST) -> list[str] | None:
    """Return the list of strings in ``__all__`` — None if it's missing/non-literal."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    value = node.value
                    if isinstance(value, (ast.List, ast.Tuple)):
                        try:
                            return [
                                el.value for el in value.elts if isinstance(el, ast.Constant) and isinstance(el.value, str)
                            ]
                        except Exception:  # pragma: no cover
                            return None
                    return None
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "__all__":
                value = node.value
                if isinstance(value, (ast.List, ast.Tuple)):
                    return [
                        el.value for el in value.elts if isinstance(el, ast.Constant) and isinstance(el.value, str)
                    ]
                return None
    return None


# TODO(next-agent): replace this grep-based check with full import-linter
# integration. The scaffold from `axi ext init` should emit a .importlinter
# contract; `axi ext validate` should run `lint-imports` against it and
# surface the structured findings. For this iteration we only flag the
# obvious case: `from axiom.<something>._<private> import ...` — see AEOS §7.4.
_PRIVATE_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+axiom(?:\.[A-Za-z0-9_]+)*\._[A-Za-z0-9_]+\s+import|"
    r"import\s+axiom(?:\.[A-Za-z0-9_]+)*\._[A-Za-z0-9_]+)",
    re.MULTILINE,
)


def _check_forbidden_imports(
    ext_path: Path, manifest: dict[str, Any]
) -> list[ValidationResult]:
    """Flag imports of Axiom core private modules (prefixed with ``_``)."""
    pkg_name = manifest.get("extension", {}).get("name") or ext_path.name
    pkg_root = ext_path / pkg_name
    if not pkg_root.exists():
        return []

    offenders: list[str] = []
    for py in pkg_root.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _PRIVATE_IMPORT_RE.finditer(text):
            rel = py.relative_to(ext_path)
            offenders.append(f"{rel}: {match.group(0).strip()}")

    if offenders:
        return [
            ValidationResult(
                check="forbidden_imports",
                ok=False,
                detail=(
                    f"{len(offenders)} private-axiom import(s) detected: "
                    + "; ".join(offenders[:3])
                    + (", ..." if len(offenders) > 3 else "")
                ),
                remediation=(
                    "import only from axiom's public API; if you need a private "
                    "module, file an issue to promote it"
                ),
            )
        ]
    return [
        ValidationResult(
            check="forbidden_imports",
            ok=True,
            detail="no private-axiom imports detected (grep-based; full import-linter pending)",
        )
    ]


def run_standard_tests(ext_path: Path) -> ValidationResult:
    """Invoke pytest against the standard test file."""
    std_test = ext_path / "tests" / "unit_tests" / "test_standard.py"
    if not std_test.exists():
        return ValidationResult(
            check="standard_tests",
            ok=False,
            detail="tests/unit_tests/test_standard.py not found",
            remediation="create the file; inherit from axiom_tests.unit_tests.ExtensionStandardTests",
        )

    # Use --rootdir=ext_path so pytest does not try to combine the extension's
    # test collection with whatever pytest config sits above on the filesystem.
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(std_test),
        "--rootdir",
        str(ext_path),
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    ok = proc.returncode == 0
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
    return ValidationResult(
        check="standard_tests",
        ok=ok,
        detail=("standard tests passed" if ok else "standard tests failed: " + " | ".join(tail)),
        remediation=(
            "" if ok else "run `axi ext test` locally and fix the failures"
        ),
    )


class ValidateProvider:
    """Built-in provider for ``axi ext validate [<path>]``."""

    verb = "validate"
    description = "Deeper conformance: entry points, public API, standard tests"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Path to the extension (default: current working directory)",
        )
        parser.add_argument("--json", action="store_true", help="Emit results as JSON")
        parser.add_argument(
            "--skip-tests",
            action="store_true",
            help="Skip running the standard pytest suite (useful in CI prechecks)",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        target = Path(args.path).resolve() if args.path else context.cwd
        results = validate_extension(target)
        if not args.skip_tests:
            results.append(run_standard_tests(target))

        failed = [r for r in results if not r.ok]

        if args.json:
            print(
                json.dumps(
                    {
                        "extension": str(target),
                        "results": [asdict(r) for r in results],
                        "failed_count": len(failed),
                    },
                    indent=2,
                )
            )
            return 1 if failed else 0

        con = console()
        con.print(f"axi ext validate: {target.name}")
        con.print("")
        for r in results:
            level = "pass" if r.ok else "fail"
            status(level, r.check, r.detail)
            if not r.ok and r.remediation:
                con.print(f"         → {r.remediation}")
        con.print("")
        if failed:
            con.print(f"{len(failed)} check(s) failed.")
            return 1
        con.print("All checks passed.")
        con.print("")
        next_steps(
            [
                "axi ext test                 # Full test suite",
                "axi ext publish --yes        # Sign + register locally",
            ]
        )
        return 0


__all__ = ["ValidateProvider", "ValidationResult", "run_standard_tests", "validate_extension"]
