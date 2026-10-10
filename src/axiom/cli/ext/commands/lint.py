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
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rich.markup import escape

from axiom.cli.ext._output import console, next_steps, status
from axiom.cli.ext.provider import CliContext


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


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

#: What an extension may say about the uncertainty of the values it produces
#: (ADR-136, ``docs/specs/spec-uncertainty.md`` §2). A closed vocabulary,
#: because the whole point of the declaration is that a reader can tell these
#: four states apart — and free text cannot be checked.
#:
#: - ``carries`` — values come with their SOURCES, so aggregates are exact.
#: - ``magnitude-only`` — a scalar error bar, no correlation structure, so
#:   aggregates over them can only be bounded.
#: - ``none`` — produces measured values and declares nothing about them.
#:   A legitimate state to be IN and not one to be silent about: every
#:   aggregate over those values reports no uncertainty while nothing fails.
#: - ``not-applicable`` — produces no measured or modelled values at all.
UNCERTAINTY_POSTURES = ("carries", "magnitude-only", "none", "not-applicable")

#: Capability kinds whose output lands in the served tier as a value. An
#: extension providing one of these is making a measurement claim, so silence
#: about its uncertainty is a gap rather than a non-question.
_VALUE_PRODUCING_KINDS = ("normalizer", "emitter")

#: How a long-running service is replaced without going down (ADR-182).
#: ``restart`` is deliberately absent: it is down for its duration, which is
#: the thing this contract exists to remove.
AVAILABILITY_SWITCHES = ("blue-green", "overlap", "stateless-rolling")
#: Whether the old and new versions can share the schema during a switch.
AVAILABILITY_MIGRATIONS = ("expand-contract", "none")
_AVAILABILITY_REQUIRED = ("switch", "readiness", "drain_s", "migrations")


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
_BUILTIN_REQUIRED_FILES: tuple[str, ...] = ("axiom-extension.toml",)

# Compound-layout subdirectory per capability kind (AEOS §5.1). AEOS040
# requires a kind's directory only when the manifest PROVIDES that kind —
# empty ceremony directories are not conformance (AEOS §5.3).
_KIND_DIRS: dict[str, str] = {
    "agent": "agents",
    "tool": "tools",
    "cmd": "commands",
    "service": "services",
    "adapter": "adapters",
    "skill": "skills",
    "hook": "hooks",
}


def lint_extension(ext_path: Path) -> list[Finding]:
    """Run Bronze conformance checks against ``ext_path``. Return findings."""
    findings: list[Finding] = []

    if not ext_path.exists() or not ext_path.is_dir():
        return [
            _error(
                code="AEOS001",
                message=f"not a directory: {ext_path}",
                remediation=f"point `{_brand_cli()} ext lint` at the extension's root directory",
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
                    remediation='fix TOML syntax; run `python -c \'import tomllib; tomllib.load(open("axiom-extension.toml", "rb"))\'`',
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
                        f"create {name} at the extension root; see AEOS §5.2 for required files"
                    ),
                )
            )

    # Bail if the manifest is missing — later checks depend on it.
    if not manifest_path.exists():
        return findings

    findings.extend(_check_uncertainty_posture(manifest))
    findings.extend(_check_availability(manifest, ext_path))

    schema_checked, schema_errors = _validate_schema(manifest)
    if not schema_checked:
        # A check that could not run is not a check that failed. Reporting it
        # as an error made `axi ext lint` impossible to pass from a released
        # wheel, which in turn blocked `quickstart`, `validate` and `publish`
        # — the whole authoring ladder, for everyone outside this repo.
        findings.append(
            _warn(
                code="AEOS023",
                message=(
                    "manifest schema NOT CHECKED: the AEOS schema validator "
                    "(axiom-tests) is not installed. Every other rule ran."
                ),
                remediation=(
                    "nothing to fix if you are authoring against a released "
                    "axiom wheel — axiom-tests is development infrastructure, "
                    "is not on PyPI, and the schema is re-checked by CI when "
                    "you publish. To run it locally, work from an axiom "
                    "source checkout, where `packages/axiom-tests` installs "
                    "with the dev environment."
                ),
            )
        )
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
                    f"manifest name {declared_name!r} does not match directory name {dir_name!r}"
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
                    f"missing Python package {declared_name or dir_name!r} next to the manifest"
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
    # Only kinds the manifest provides require their directory (§5.3).
    layout_root = ext_path if (is_builtin and init_path == pkg_init_flat) else pkg_dir_compound
    provided_kinds = {
        p.get("kind") for p in (ext_block.get("provides") or []) if isinstance(p, dict)
    }
    for kind, dirname in _KIND_DIRS.items():
        if kind not in provided_kinds:
            continue
        if not (layout_root / dirname).is_dir():
            findings.append(
                _warn(
                    code="AEOS040",
                    message=(
                        f"manifest provides {kind!r} but the {dirname}/ subdirectory is missing"
                    ),
                    remediation=(
                        # {dirname}, not {kind}: the check above looks for
                        # {dirname}/, and this branch's version said {kind}/ —
                        # which would have sent someone to create a directory
                        # the linter does not look at.
                        f"create {layout_root.name}/{dirname}/ and keep {kind} "
                        f"capabilities there (AEOS §5.1; provided kinds only per §5.3); "
                        f"the scaffold from `{_brand_cli()} ext init` includes it"
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

    # -- [extension.secrets] requires — named secret dependencies (#667) --
    findings.extend(_secret_dependency_findings(manifest))

    # -- [extension.mcp] block (spec-builtin-mcp-server.md §6.3) ----------
    findings.extend(_mcp_block_findings(manifest_path))
    findings.extend(_mcp_exposure_findings(manifest, ext_path))

    # -- CLI output goes through cli_format, here as in the platform ------
    findings.extend(_cli_format_findings(ext_path))

    return findings


_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _cli_format_findings(ext_path: Path) -> list[Finding]:
    """Hand-rolled CLI layout inside an extension.

    A warning, not an error. An extension that draws its own table is not
    broken — it is inconsistent with every other verb the operator sees,
    and inconsistency in a composed surface is the thing nobody owns and
    everybody notices.

    This is the same rule the platform holds itself to. A rule that differs
    between the code we write and the code we accept is two rules, and the
    composed-in extension is exactly where a second one would go unnoticed.
    """
    from axiom.infra.cli_format_lint import scan_tree

    offences = scan_tree(ext_path)
    if not offences:
        return []
    first = offences[0]
    where = f"{first.path.name}:{first.line}"
    more = f" (+{len(offences) - 1} more)" if len(offences) > 1 else ""
    return [
        _warn(
            code="AEOS310",
            message=(
                f"{len(offences)} hand-rolled CLI layout(s), first at "
                f"{where}{more} — a fixed column width cannot adapt to "
                f"content and shifts every column after it"
            ),
            remediation=first.remedy,
        )
    ]


def _secret_dependency_findings(manifest: dict[str, Any]) -> list[Finding]:
    """Check ``[extension.secrets] requires`` (issue #667).

    Extensions declare the named foreign credentials they need (webhook
    URLs, HMAC keys, PATs) so deploy-time wiring is auditable:

        [extension.secrets]
        requires = ["herald-webhook-hmac"]

    Shape violations are errors (AEOS080). A declared secret missing
    from THIS machine's foreign-credential store is a warning (AEOS081)
    — installs differ; Bronze conformance must not depend on local
    state. The store lookup reads the metadata index only (names; no
    keychain access, no values).
    """
    block = manifest.get("extension", {}).get("secrets")
    if block is None:
        return []
    out: list[Finding] = []
    requires = block.get("requires") if isinstance(block, dict) else None
    if (
        not isinstance(block, dict)
        or not isinstance(requires, list)
        or not all(isinstance(n, str) for n in (requires or []))
    ):
        return [
            _error(
                code="AEOS080",
                message=(
                    "[extension.secrets] must be a table with "
                    '`requires = ["name", ...]` (list of strings)'
                ),
                remediation=(
                    "declare named secret dependencies as a string list, "
                    'e.g. requires = ["my-webhook-hmac"]'
                ),
            )
        ]
    bad = [n for n in requires if not _SECRET_NAME_RE.match(n)]
    if bad:
        out.append(
            _error(
                code="AEOS080",
                message=f"invalid secret dependency name(s): {bad}",
                remediation=(
                    "secret names use letters/digits/._- and start "
                    "alphanumeric (they become keychain account names)"
                ),
            )
        )
        requires = [n for n in requires if n not in bad]

    # Local-store presence check — best-effort, warning-only.
    try:
        from axiom.extensions.builtins.secrets.foreign.store import (
            declared_secret_names,
        )
        from axiom.infra.paths import get_user_state_dir

        present = set(declared_secret_names(get_user_state_dir()))
    except Exception:  # noqa: BLE001 — never fail lint on store trouble
        return out
    missing = sorted(set(requires) - present)
    if missing:
        out.append(
            _warn(
                code="AEOS081",
                message=(
                    "declared secret dependencies not present in this "
                    f"machine's secret store: {', '.join(missing)}"
                ),
                remediation=(
                    f"store each with `{_brand_cli()} secrets set <name>` (value via "
                    "stdin/prompt) before deploying this extension here"
                ),
            )
        )
    return out


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
                # One line, so no escape has to stand in for a newline. This
                # carried a literal backslash-n, which is what the reader saw,
                # and it reads as a typo in the tool rather than as a newline.
                "add an [extension.mcp] block with `enabled = true` (or "
                "`enabled = false` to opt out) to the manifest, OR add a one-line "
                "comment above [extension] of the form "
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
            remediation = "see spec-builtin-mcp-server.md §7 for valid [extension.mcp] schema"

        if isinstance(finding, LintError):
            out.append(_error(code=code, message=message, remediation=remediation))
        elif isinstance(finding, LintWarning):
            out.append(_warn(code=code, message=message, remediation=remediation))
        else:  # defensive
            out.append(_warn(code=code, message=str(finding), remediation=remediation))

    return out


def _mcp_exposure_findings(manifest: dict[str, Any], ext_path: Path) -> list[Finding]:
    """A provided tool reaches an assistant over MCP, or lint says why not (#1161).

    - ``AEOS074``: ``[extension.mcp]`` is enabled and a ``provides`` tool has
      no ``[[extension.mcp.tool]]`` entry, so it is silently absent from the
      assistant's tool list.
    - ``AEOS075``: an exposed tool's handler does not take exactly one
      positional argument. MCP calls it with one dict of arguments, so a
      handler with keyword parameters fails only when an assistant calls it.

    The handler is read with ``ast``; lint never imports extension code.
    """
    ext = manifest.get("extension") or {}
    mcp = ext.get("mcp") or {}
    if not mcp.get("enabled"):
        return []
    listed = {str(t.get("name") or "") for t in (mcp.get("tool") or []) if isinstance(t, dict)}
    # A tool whose verb is also a registered skill (``ext.verb`` for the tool
    # ``ext_verb``) reaches MCP through the registry projection instead.
    listed |= {
        str(p.get("name") or "").replace(".", "_")
        for p in (ext.get("provides") or [])
        if isinstance(p, dict) and p.get("kind") == "skill"
    }
    out: list[Finding] = []
    for p in ext.get("provides") or []:
        if not isinstance(p, dict) or p.get("kind") != "tool" or not p.get("name"):
            continue
        name = str(p["name"])
        if name not in listed:
            out.append(
                _warn(
                    code="AEOS074",
                    message=(
                        f"tool {name!r} is provided but not exposed over MCP: "
                        f"[extension.mcp] is enabled and has no [[extension.mcp.tool]] "
                        f"entry named {name!r}, so no assistant will see it"
                    ),
                    remediation=(
                        f'add `[[extension.mcp.tool]]` with `name = "{name}"` to the '
                        "manifest, or leave it out on purpose and ignore this warning"
                    ),
                )
            )
            continue
        problem = _handler_arity_problem(ext_path, str(p.get("entry") or ""))
        if problem:
            out.append(
                _warn(
                    code="AEOS075",
                    message=(
                        f"MCP handler for {name!r} {problem}; MCP calls it with one dict "
                        "of arguments, so this fails when an assistant calls the tool"
                    ),
                    remediation=(
                        "make the handler take one positional parameter, e.g. "
                        "`def handler(args: dict) -> dict:`, and read fields from it"
                    ),
                )
            )
    return out


def _handler_arity_problem(ext_path: Path, entry: str) -> str:
    """Why ``entry`` (``pkg.mod:func``) is not a one-dict handler, or ""."""
    import ast

    module, _, func = entry.partition(":")
    if not module or not func:
        return ""
    rel = Path(*module.split("."))
    candidates = [
        ext_path / f"{rel}.py",
        ext_path / rel / "__init__.py",
        ext_path / "src" / f"{rel}.py",
        ext_path / "src" / rel / "__init__.py",
    ]
    source = next((c for c in candidates if c.is_file()), None)
    if source is None:
        return ""  # not in this tree (e.g. a platform module): nothing to check
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return ""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            a = node.args
            positional = [*a.posonlyargs, *a.args]
            required_kwonly = [k for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
            if a.vararg is not None and not positional:
                return ""
            if len(positional) != 1 or required_kwonly:
                names = ", ".join(x.arg for x in positional + required_kwonly) or "nothing"
                return f"takes ({names})"
            return ""
    return ""


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


def _readiness_ok(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if value in ("tcp", "process"):
        return True
    return value.startswith("http:/") and len(value) > len("http:/")


def _check_availability(manifest: dict[str, Any], ext_path: Path) -> list[Finding]:
    """Does each long-running service say how it stays up through a change?

    ADR-182: Axiom does not go down from its own causes (deploys, updates,
    migrations, configuration changes). That holds only if every service
    declares how it is switched, how it reports ready, how long it drains,
    and whether its schema changes can be shared by the old and new versions
    at once. Silence is an error here, unlike AEOS091, because a service
    nobody has thought about is exactly the one that goes down.

    Proof is graded separately. ``verified_by = "pending"`` is a warning
    (AEOS102): the fleet declares honestly now and proves one service at a
    time. A ``verified_by`` path that does not exist is an error (AEOS103),
    because it reads as evidence.
    """
    ext = manifest.get("extension", {})
    provides = ext.get("provides") or []
    kinds = {str(c.get("kind", "")).lower() for c in provides if isinstance(c, dict)}
    if "service" not in kinds:
        return []

    block = ext.get("availability")
    if not isinstance(block, dict):
        return [
            _error(
                code="AEOS100",
                message="provides a service but declares no [extension.availability]",
                remediation=(
                    "add [extension.availability] with switch ("
                    + ", ".join(AVAILABILITY_SWITCHES)
                    + "), readiness (http:<path>, tcp or process), drain_s, migrations ("
                    + ", ".join(AVAILABILITY_MIGRATIONS)
                    + '), single_flight and verified_by ("pending" or the zero-gap '
                    "upgrade test). ADR-182: a service is never down from our own causes"
                ),
            )
        ]

    problems: list[str] = []
    missing = [k for k in _AVAILABILITY_REQUIRED if k not in block]
    if missing:
        problems.append("missing " + ", ".join(missing))
    if "switch" in block and block["switch"] not in AVAILABILITY_SWITCHES:
        problems.append(
            f"switch = {block['switch']!r} is not one of " + ", ".join(AVAILABILITY_SWITCHES)
        )
    if "readiness" in block and not _readiness_ok(block["readiness"]):
        problems.append(f"readiness = {block['readiness']!r} must be http:<path>, tcp or process")
    if "drain_s" in block:
        d = block["drain_s"]
        if isinstance(d, bool) or not isinstance(d, (int, float)) or d < 0:
            problems.append(f"drain_s = {d!r} must be a non-negative number of seconds")
    if "migrations" in block and block["migrations"] not in AVAILABILITY_MIGRATIONS:
        problems.append(
            f"migrations = {block['migrations']!r} is not one of "
            + ", ".join(AVAILABILITY_MIGRATIONS)
        )
    if "single_flight" in block and not isinstance(block["single_flight"], bool):
        problems.append("single_flight must be true or false")

    findings: list[Finding] = []
    if problems:
        findings.append(
            _error(
                code="AEOS101",
                message="[extension.availability]: " + "; ".join(problems),
                remediation="see docs/specs/spec-aeos-0.1.md §6.5 and ADR-182",
            )
        )

    proof = block.get("verified_by", "pending")
    if proof == "pending" or proof is None:
        findings.append(
            _warn(
                code="AEOS102",
                message="availability declared but not yet proven (verified_by = pending)",
                remediation=(
                    "write the zero-gap upgrade test for this service (0 refused "
                    "requests, 0 lost or duplicated work across a switch) and name "
                    "it in verified_by"
                ),
            )
        )
    elif not isinstance(proof, str) or not (ext_path / proof).exists():
        findings.append(
            _error(
                code="AEOS103",
                message=f"verified_by = {proof!r} names a file that does not exist",
                remediation="point verified_by at the zero-gap upgrade test, relative to the extension",
            )
        )
    return findings


def _check_uncertainty_posture(manifest: dict[str, Any]) -> list[Finding]:
    """Does this extension say anything about the uncertainty of its values?

    ADR-136 makes uncertainty a platform primitive and
    ``docs/specs/spec-uncertainty.md`` §2 states the contract a contributing
    extension implements. A contract nothing checks is a suggestion, and the
    failure mode it guards against is specific: an extension can produce
    values with no uncertainty at all, every aggregate over them reports
    ``claimable: false``, and **nothing anywhere fails**. The absence is
    invisible precisely because it is an absence.

    Two findings, deliberately different in severity:

    - A posture outside the closed vocabulary is an **error**. A declared
      surface that is misspelled is worse than one that is missing, because
      a reader believes it.
    - A value-producing capability with no posture is a **warning**, not an
      error. This check is new and no extension in the fleet has declared
      one yet; erroring would fail lint everywhere at once and teach people
      to pass ``--no-verify``. It becomes an error when the fleet has
      declared — the ratchet, not the cliff.

    An extension that produces no values says ``not-applicable`` and is done.
    Saying nothing is not the same as saying that.
    """
    findings: list[Finding] = []
    ext = manifest.get("extension", {})
    block = ext.get("uncertainty")
    posture = None
    if isinstance(block, dict):
        posture = block.get("posture")
    elif isinstance(block, str):
        # Tolerated shorthand: `uncertainty = "carries"`. Accepted because
        # refusing it would be pedantry about a declaration we want made.
        posture = block

    if posture is not None and posture not in UNCERTAINTY_POSTURES:
        findings.append(
            _error(
                code="AEOS090",
                message=(
                    f"[extension.uncertainty] posture = {posture!r} is not one of "
                    f"{', '.join(UNCERTAINTY_POSTURES)}"
                ),
                remediation=(
                    "use one of the four; see docs/specs/spec-uncertainty.md §2. "
                    "A misspelled posture reads as a declaration and is believed"
                ),
            )
        )
        return findings

    if posture is None:
        provides = ext.get("provides") or manifest.get("extension", {}).get("provides") or []
        kinds = {str(c.get("kind", "")).lower() for c in provides if isinstance(c, dict)}
        producing = sorted(kinds & set(_VALUE_PRODUCING_KINDS))
        if producing:
            findings.append(
                _warn(
                    code="AEOS091",
                    message=(
                        f"provides {', '.join(producing)} but declares no "
                        "[extension.uncertainty] posture"
                    ),
                    remediation=(
                        'add `[extension.uncertainty]` with `posture = "carries"`, '
                        '"magnitude-only", "none" or "not-applicable" '
                        "(ADR-136; docs/specs/spec-uncertainty.md §2). Values served "
                        "with no uncertainty report none and nothing fails, so the "
                        "absence has to be declared rather than discovered"
                    ),
                )
            )
    return findings


def _validate_schema(manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate against the AEOS JSON Schema shipped with ``axiom-tests``.

    Returns ``(ran, errors)``. The flag matters: ``(True, [])`` means the
    manifest was checked and is valid, while ``(False, [])`` means it was
    never checked at all. Collapsing those two into one empty list is what
    produced the bug this signature exists to prevent — the absent validator
    was returned as though it were a violation, and an author with a perfectly
    good manifest was told their manifest was malformed.

    ``axiom-tests`` is not published to PyPI; it lives in this repo under
    ``packages/axiom-tests`` and reaches contributors through an editable
    install. So its absence is the NORMAL state for anyone authoring an
    extension against a released wheel, and must not read as their mistake.

    Import is local — `axi ext lint` is the only caller, and we avoid paying
    the import cost during generic CLI startup.
    """
    try:
        from axiom_tests import validate_manifest
    except ImportError:
        return False, []
    return True, validate_manifest(manifest)


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
    description = "Verify Bronze-level AEOS conformance (layout + manifest; AEOS §12.1)"

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
            con.print(f"{_brand_cli()} ext lint: {target.name}: OK (Bronze — layout + manifest ok)")
            con.print("")
            next_steps(
                [
                    f"{_brand_cli()} ext test                 # Run the standard tests",
                    f"{_brand_cli()} ext scan                 # Pre-publish policy gate",
                ]
            )
            return 0

        con.print(f"{_brand_cli()} ext lint: {target.name}: {len(findings)} finding(s)")
        con.print("")
        for f in findings:
            level = "fail" if f.severity == "error" else "warn"
            status(level, f.code, f.message)
            # Escaped for the same reason `status` escapes its detail: a
            # remediation naming a TOML table is the common case here, and
            # rich would eat the table.
            con.print(f"          → {escape(f.remediation)}")
            con.print("")
        return 1 if errors else 0


__all__ = ["Finding", "LintProvider", "lint_extension"]
