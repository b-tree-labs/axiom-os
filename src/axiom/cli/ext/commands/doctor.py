# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext doctor`` — one-stop diagnostic sweep.

Doctor aggregates three already-existing verbs (``lint``, ``validate``,
``test``) plus a handful of fast environment checks into a single table so
extension authors can see at a glance whether anything is wrong. It does
**not** reimplement any of those verbs' logic — it invokes their providers
in-process and folds the structured results into a common :class:`DoctorResult`
stream.

Why a separate verb instead of just running each in sequence? Two reasons:

1. Environment checks (Python version, ``axiom-tests`` importable) are
   common preconditions for the other verbs; diagnosing "my lint failed
   because the interpreter is too old" is faster when a single command
   surfaces both.
2. CI callers want one non-zero exit for "anything is wrong" — chaining
   three commands with ``&&`` obscures *which* check failed when reading
   a pipeline log.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from axiom.cli.ext._output import console, status
from axiom.cli.ext.commands.lint import lint_extension
from axiom.cli.ext.commands.validate import run_standard_tests, validate_extension
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.templates.scaffold import placeholder_module_body

# The minimum Python required by AEOS 0.1 (spec §6.1 default compatibility).
# Doctor flags anything below this as a failing environment check.
_MIN_PYTHON: tuple[int, int] = (3, 11)


@dataclass(frozen=True)
class DoctorResult:
    """A single doctor finding. Mirrors ValidationResult for uniformity."""

    check: str
    ok: bool
    detail: str
    remediation: str = ""


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------


def _check_python_version() -> DoctorResult:
    major, minor = sys.version_info.major, sys.version_info.minor
    ok = (major, minor) >= _MIN_PYTHON
    return DoctorResult(
        check="python_version",
        ok=ok,
        detail=f"Python {major}.{minor} (need >= {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]})",
        remediation=(
            "" if ok else f"install Python {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+"
        ),
    )


def _check_axiom_tests_importable() -> DoctorResult:
    try:
        import axiom_tests  # noqa: F401

        return DoctorResult(
            check="axiom_tests_importable",
            ok=True,
            detail=f"axiom-tests {getattr(__import__('axiom_tests'), '__version__', 'unknown')} importable",
        )
    except Exception as exc:  # noqa: BLE001 — diagnostic catch
        return DoctorResult(
            check="axiom_tests_importable",
            ok=False,
            detail=f"cannot import axiom_tests: {exc}",
            remediation="pip install axiom-tests",
        )


def _check_manifest_parses(ext_path: Path) -> DoctorResult:
    manifest = ext_path / "axiom-extension.toml"
    if not manifest.exists():
        return DoctorResult(
            check="manifest_parses",
            ok=False,
            detail=f"axiom-extension.toml not found at {manifest}",
            remediation="run `axi ext init <name>` or create the manifest",
        )
    try:
        with manifest.open("rb") as fh:
            tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001
        return DoctorResult(
            check="manifest_parses",
            ok=False,
            detail=f"manifest TOML parse failed: {exc}",
            remediation="fix the TOML syntax; see AEOS §6 for the schema",
        )
    return DoctorResult(
        check="manifest_parses", ok=True, detail="axiom-extension.toml parses cleanly"
    )


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def run_doctor(ext_path: Path, *, skip_tests: bool = False) -> list[DoctorResult]:
    """Run the full doctor sweep against ``ext_path``.

    ``skip_tests=True`` elides the pytest invocation (keeps CI precheck fast).
    """
    results: list[DoctorResult] = [
        _check_python_version(),
        _check_axiom_tests_importable(),
        _check_manifest_parses(ext_path),
    ]

    # -- lint ----------------------------------------------------------------
    lint_findings = lint_extension(ext_path)
    lint_errors = [f for f in lint_findings if f.severity == "error"]
    results.append(
        DoctorResult(
            check="lint",
            ok=not lint_errors,
            detail=(
                "no lint errors" if not lint_errors
                else f"{len(lint_errors)} lint error(s): "
                + "; ".join(f"{f.code} {f.message}" for f in lint_errors[:3])
                + (", ..." if len(lint_errors) > 3 else "")
            ),
            remediation=(
                "" if not lint_errors else "run `axi ext lint` for full findings"
            ),
        )
    )

    # -- validate (entry-point + public API + forbidden imports) ------------
    # Validate requires both manifest and pyproject to exist and parse; if
    # either is malformed we skip the deeper checks — lint already surfaced
    # the parse error and re-raising here would mask every other finding.
    try:
        validate_results = validate_extension(ext_path)
    except Exception as exc:  # noqa: BLE001 — diagnostic catch
        results.append(
            DoctorResult(
                check="validate",
                ok=False,
                detail=f"validate aborted: {exc}",
                remediation="fix the manifest/pyproject parse error surfaced by lint",
            )
        )
    else:
        for r in validate_results:
            results.append(
                DoctorResult(
                    check=f"validate.{r.check}",
                    ok=r.ok,
                    detail=r.detail,
                    remediation=r.remediation,
                )
            )

    # -- standard tests (optional) ------------------------------------------
    if not skip_tests:
        tests_dir = ext_path / "tests"
        if not tests_dir.exists():
            results.append(
                DoctorResult(
                    check="tests",
                    ok=False,
                    detail=f"no tests/ directory at {tests_dir}",
                    remediation="scaffold tests via `axi ext init` or add them manually",
                )
            )
        else:
            # Prefer the focused standard-test run over a full pytest sweep —
            # doctor is a triage tool, not the primary test runner.
            std = run_standard_tests(ext_path)
            results.append(
                DoctorResult(
                    check="tests",
                    ok=std.ok,
                    detail=std.detail,
                    remediation=std.remediation,
                )
            )

    return results


# ---------------------------------------------------------------------------
# Auto-fix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixAction:
    """A single remediable issue + a callable that performs the fix.

    ``describe`` is a short human-friendly label used for narration; ``apply``
    is a zero-arg callable that performs the side-effect.
    """

    label: str
    describe: str
    apply: callable  # type: ignore[name-defined]


def _iter_provides_entry_point_fixes(ext_path: Path) -> list[FixAction]:
    """Return fix actions for missing pyproject entry points referenced by the manifest."""
    manifest_path = ext_path / "axiom-extension.toml"
    pyproject_path = ext_path / "pyproject.toml"
    if not manifest_path.exists() or not pyproject_path.exists():
        return []

    try:
        with manifest_path.open("rb") as fh:
            manifest = tomllib.load(fh)
        with pyproject_path.open("rb") as fh:
            pyproject = tomllib.load(fh)
    except Exception:  # noqa: BLE001
        return []

    # Map capability kinds to the pyproject entry-point group names. Kept in
    # sync with validate._ENTRY_POINT_GROUPS — duplicated rather than imported
    # to avoid a circular dependency.
    kind_to_group = {
        "agent": "axiom.agents",
        "tool": "axiom.tools",
        "cmd": "axiom.commands",
        "service": "axiom.services",
        "adapter": "axiom.adapters",
        "hook": "axiom.hooks",
        "signal_type": "axiom.signal_types",
    }

    ep_table = (pyproject.get("project") or {}).get("entry-points") or {}
    fixes: list[FixAction] = []
    for block in (manifest.get("extension") or {}).get("provides", []) or []:
        kind = block.get("kind")
        entry = block.get("entry")
        label = (
            block.get("name") or block.get("noun") or block.get("integration")
        )
        if not kind or not entry or not label:
            continue
        group = kind_to_group.get(kind)
        if group is None:
            continue
        existing = (ep_table.get(group) or {}).get(label)
        if existing == entry:
            continue

        def _apply(g=group, lbl=label, ent=entry) -> None:
            _add_entry_point(pyproject_path, group=g, label=lbl, target=ent)

        fixes.append(
            FixAction(
                label=f"entry_point[{kind}:{label}]",
                describe=(
                    f"add [project.entry-points.\"{group}\"] {label} = \"{entry}\" "
                    "to pyproject.toml"
                ),
                apply=_apply,
            )
        )
    return fixes


def _add_entry_point(
    pyproject_path: Path, *, group: str, label: str, target: str
) -> None:
    """Idempotently add an entry point line to the given pyproject.toml group."""
    text = pyproject_path.read_text(encoding="utf-8")
    header = f'[project.entry-points."{group}"]'
    new_line = f'{label} = "{target}"'
    if header in text:
        # Insert right after the header if the label is absent.
        lines = text.splitlines(keepends=True)
        out: list[str] = []
        i = 0
        inserted = False
        while i < len(lines):
            out.append(lines[i])
            if lines[i].rstrip() == header and not inserted:
                # Check subsequent lines in this block for an existing assignment.
                block_end = i + 1
                while block_end < len(lines) and not lines[block_end].lstrip().startswith("["):
                    if lines[block_end].lstrip().startswith(f"{label} ="):
                        break
                    block_end += 1
                else:
                    # Fall through — no matching assignment found.
                    out.append(new_line + "\n")
                    inserted = True
            i += 1
        if not inserted:
            # Header already had content but no matching label — append the
            # line at the end of the file.
            if not text.endswith("\n"):
                out.append("\n")
            out.append(new_line + "\n")
        pyproject_path.write_text("".join(out), encoding="utf-8")
    else:
        sep = "" if text.endswith("\n") else "\n"
        pyproject_path.write_text(
            text + sep + f"\n{header}\n{new_line}\n", encoding="utf-8"
        )


def _placeholder_module_fix(ext_path: Path) -> list[FixAction]:
    """If the manifest references the placeholder module but it's gone, recreate it."""
    manifest_path = ext_path / "axiom-extension.toml"
    if not manifest_path.exists():
        return []
    try:
        with manifest_path.open("rb") as fh:
            manifest = tomllib.load(fh)
    except Exception:  # noqa: BLE001
        return []
    name = (manifest.get("extension") or {}).get("name") or ext_path.name
    commands_dir = ext_path / name / "commands"
    placeholder = commands_dir / "placeholder.py"
    # Only recreate when the manifest still points at the placeholder module.
    refs_placeholder = False
    for block in (manifest.get("extension") or {}).get("provides", []) or []:
        entry = block.get("entry") or ""
        if entry.endswith("commands.placeholder:cli"):
            refs_placeholder = True
            break
    if not refs_placeholder or placeholder.exists():
        return []

    def _apply() -> None:
        commands_dir.mkdir(parents=True, exist_ok=True)
        init_py = commands_dir / "__init__.py"
        if not init_py.exists():
            init_py.write_text(
                "# Copyright (c) 2026 The University of Texas at Austin\n"
                "# Copyright (c) 2026 B-Tree Labs\n"
                "# SPDX-License-Identifier: Apache-2.0\n",
                encoding="utf-8",
            )
        placeholder.write_text(placeholder_module_body(name), encoding="utf-8")

    return [
        FixAction(
            label="placeholder_module",
            describe=f"recreate {placeholder.relative_to(ext_path)}",
            apply=_apply,
        )
    ]


def _copyright_year_fix(ext_path: Path) -> list[FixAction]:
    """Bump the copyright line in LICENSE to the current year when it's stale."""
    lic = ext_path / "LICENSE"
    if not lic.exists():
        return []
    try:
        text = lic.read_text(encoding="utf-8")
    except OSError:
        return []
    match = re.search(r"^(\s*Copyright\s+)(\d{4})\b", text, flags=re.MULTILINE)
    if not match:
        return []
    year = int(match.group(2))
    current = _dt.datetime.now().year
    if year >= current:
        return []

    def _apply() -> None:
        updated = re.sub(
            r"^(\s*Copyright\s+)(\d{4})\b",
            lambda m: f"{m.group(1)}{current}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        lic.write_text(updated, encoding="utf-8")

    return [
        FixAction(
            label="copyright_year",
            describe=f"bump LICENSE copyright year {year} -> {current}",
            apply=_apply,
        )
    ]


def _py_typed_fix(ext_path: Path) -> list[FixAction]:
    """PEP 561 marker — recreate it when missing."""
    manifest_path = ext_path / "axiom-extension.toml"
    if not manifest_path.exists():
        return []
    try:
        with manifest_path.open("rb") as fh:
            manifest = tomllib.load(fh)
    except Exception:  # noqa: BLE001
        return []
    name = (manifest.get("extension") or {}).get("name") or ext_path.name
    pkg = ext_path / name
    if not pkg.is_dir():
        return []
    marker = pkg / "py.typed"
    if marker.exists():
        return []

    def _apply() -> None:
        marker.write_text("", encoding="utf-8")

    return [
        FixAction(
            label="py_typed",
            describe=f"recreate {marker.relative_to(ext_path)}",
            apply=_apply,
        )
    ]


def collect_fix_actions(ext_path: Path) -> list[FixAction]:
    """Return every fixable action for ``ext_path`` in a deterministic order."""
    actions: list[FixAction] = []
    actions.extend(_iter_provides_entry_point_fixes(ext_path))
    actions.extend(_placeholder_module_fix(ext_path))
    actions.extend(_copyright_year_fix(ext_path))
    actions.extend(_py_typed_fix(ext_path))
    return actions


def apply_fixes(
    ext_path: Path, *, dry_run: bool = False
) -> tuple[list[FixAction], list[FixAction]]:
    """Apply every fixable action. Returns (applied, still_broken).

    ``still_broken`` is the list of actions that were detected *after* applying
    the first pass (indicates a residual issue needs manual attention).
    """
    actions = collect_fix_actions(ext_path)
    if dry_run:
        return actions, []

    for act in actions:
        act.apply()

    # One more pass to confirm each fix held. Anything left is surfaced as
    # residual so the caller knows --fix didn't close the loop.
    residual = collect_fix_actions(ext_path)
    return actions, residual


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class DoctorProvider:
    """Built-in provider for ``axi ext doctor [<path>]``."""

    verb = "doctor"
    description = "Aggregate lint + validate + test + environment diagnostics"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Path to the extension (default: current working directory)",
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit results as JSON",
        )
        parser.add_argument(
            "--skip-tests",
            action="store_true",
            help="Skip the standard pytest run (environment + lint + validate only)",
        )
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Attempt auto-fixes for known remediable issues, then re-check",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="With --fix, narrate the proposed fixes without writing",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        target = Path(args.path).resolve() if args.path else context.cwd

        # -- --fix branch --------------------------------------------------
        if getattr(args, "fix", False):
            con = console()
            con.print(f"axi ext doctor --fix: {target.name}")
            con.print("")
            actions, residual = apply_fixes(
                target, dry_run=getattr(args, "dry_run", False)
            )
            if not actions:
                con.print("doctor --fix: nothing to fix")
                return 0
            for act in actions:
                status("info", act.label, act.describe)
            if args.dry_run:
                con.print("")
                con.print(
                    f"doctor --fix: {len(actions)} fix(es) proposed (dry-run; "
                    "no files changed)"
                )
                return 0
            # Re-check: residual fixes indicate incomplete remediation.
            for act in actions:
                if act.label not in {r.label for r in residual}:
                    status("pass", act.label, "fixed")
            for act in residual:
                status("fail", act.label, "still broken after fix attempt")
            con.print("")
            if residual:
                con.print(
                    f"doctor --fix: {len(actions) - len(residual)} fixed, "
                    f"{len(residual)} still broken"
                )
                return 1
            con.print(f"doctor --fix: {len(actions)} fix(es) applied")
            return 0

        results = run_doctor(target, skip_tests=args.skip_tests)
        failed = [r for r in results if not r.ok]

        if args.as_json:
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

        # Human-readable table: one row per check.
        con = console()
        con.print(f"axi ext doctor: {target.name}")
        con.print("")
        width = max((len(r.check) for r in results), default=0)
        for r in results:
            level = "pass" if r.ok else "fail"
            status(level, r.check.ljust(width), r.detail)
            if not r.ok and r.remediation:
                con.print(f"         → {r.remediation}")
        con.print("")
        if failed:
            con.print(f"{len(failed)} check(s) failed.")
            return 1
        con.print("All checks passed.")
        return 0


__all__ = [
    "DoctorProvider",
    "DoctorResult",
    "FixAction",
    "apply_fixes",
    "collect_fix_actions",
    "run_doctor",
]
