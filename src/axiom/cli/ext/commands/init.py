# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext init`` — scaffold a new AEOS-conformant compound extension.

The emitted layout is the canonical compound structure described in AEOS §5.1:
a purpose-named package directory with empty capability-kind subdirectories,
a ``tests/`` tree wired to ``axiom-tests``, a populated ``docs/`` skeleton,
and the full set of required top-level files (README, CHANGELOG, LICENSE,
AGENTS.md, pyproject.toml, axiom-extension.toml).

Running ``axi ext init <name> && axi ext lint <name>`` must pass Bronze
conformance on day one — see ``tests/cli/ext/test_init.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.prompt import Confirm, Prompt

from axiom.cli.ext._output import console, error, heading, next_steps, table
from axiom.cli.ext._spdx import allowlist_hint, resolve_spdx
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.templates import default_template, get_template
from axiom.cli.ext.templates import registry as template_registry

# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

# Extension names are reserved against these roots to avoid import collisions
# and platform ambiguity. The list is conservative — expand as needed.
_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "axiom",
        "axiom_tests",
        "extension",
        "extensions",
        "ext",
        "axi",
        "neut",
        "keplo",
        "vega",
        "vyzier",
        "tests",
        "test",
        "src",
        "docs",
    }
)

# Type suffixes the AEOS layout explicitly forbids (§5.4). Keep in sync with
# the playbook — the error message references this list.
_TYPE_SUFFIXES: tuple[str, ...] = (
    "_agent",
    "_tool",
    "_cmd",
    "_command",
    "_service",
    "_adapter",
    "_skill",
    "_hook",
)

_NAME_RE = __import__("re").compile(r"^[a-z][a-z0-9_]*$")


def validate_name(name: str) -> str | None:
    """Validate an extension name. Return ``None`` on success, else an error."""
    if not name:
        return "extension name cannot be empty"
    if not _NAME_RE.match(name):
        return (
            "extension name must be lowercase, start with a letter, and use "
            "only letters, digits, and underscores (no hyphens, no uppercase)"
        )
    for suffix in _TYPE_SUFFIXES:
        if name.endswith(suffix):
            return (
                f"extension name {name!r} ends with the type suffix {suffix!r}; "
                "per AEOS §5.4 extensions are purpose-named — put type information "
                "in the manifest's [[extension.provides]] blocks instead"
            )
    if name in _RESERVED_NAMES:
        return f"extension name {name!r} is reserved — pick a different purpose name"
    return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class InitProvider:
    """Built-in provider for ``axi ext init <name>``."""

    verb = "init"
    description = "Scaffold a new AEOS-conformant compound extension"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        # ``name`` is nargs='?' so bare ``axi ext init`` in a TTY drops into
        # the wizard. Argparse will still error cleanly when stdin is not a
        # TTY *and* --interactive wasn't passed (see run()).
        parser.add_argument(
            "name",
            nargs="?",
            default=None,
            help="Extension name (purpose-named, no type suffix)",
        )
        parser.add_argument(
            "--dir",
            dest="target_dir",
            default=None,
            help="Parent directory for the new extension (default: current working directory)",
        )
        parser.add_argument(
            "--owner",
            default="b-tree-labs",
            help="Owning organization for the manifest (e.g. b-tree-labs, ut-austin)",
        )
        parser.add_argument(
            "--license",
            default="Apache-2.0",
            help="SPDX license identifier (default: Apache-2.0)",
        )
        parser.add_argument(
            "--description",
            default="",
            help="One-line description for the manifest and pyproject.toml",
        )
        parser.add_argument(
            "--template",
            default=None,
            help="Template id from `axi ext templates` (default: the registry default)",
        )
        parser.add_argument(
            "--interactive",
            action="store_true",
            help="Walk through the scaffold decisions via Rich prompts",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        # Wizard path: explicit --interactive or a TTY invocation with no name.
        wants_wizard = getattr(args, "interactive", False) or args.name is None
        if wants_wizard:
            if not sys.stdin.isatty():
                if getattr(args, "interactive", False):
                    error(
                        "axi ext init: interactive mode requires a TTY",
                        hint="pass a name positionally, e.g. `axi ext init <name>`",
                    )
                    return 2
                # No --interactive and no name in a non-TTY context: match the
                # pre-wizard behavior with a clear argparse-like message.
                error(
                    "axi ext init: the following arguments are required: name",
                    hint="pass --interactive in a TTY to walk the wizard",
                )
                return 2
            return self._run_wizard(args, context)

        err_msg = validate_name(args.name)
        if err_msg is not None:
            error(f"axi ext init: {err_msg}")
            return 2

        resolved_license = resolve_spdx(args.license)
        if resolved_license is None:
            error(
                f"axi ext init: unknown license {args.license!r}",
                hint=f"accepted ids: {allowlist_hint()}",
            )
            return 2
        args.license = resolved_license

        if args.template is None:
            template = default_template()
        else:
            template = get_template(args.template)
            if template is None:
                error(
                    f"axi ext init: unknown template {args.template!r}",
                    hint="run `axi ext templates` to see registered templates",
                )
                return 2

        base_dir = Path(args.target_dir) if args.target_dir else context.cwd
        return _scaffold_and_report(
            base_dir,
            template=template,
            name=args.name,
            owner=args.owner,
            license=args.license,
            description=args.description,
        )

    # ------------------------------------------------------------------
    # Wizard
    # ------------------------------------------------------------------

    def _run_wizard(
        self, args: argparse.Namespace, context: CliContext
    ) -> int:
        """Prompt flow for name/owner/license/description/template + preview."""
        con = console()
        heading("axi ext init — guided scaffold")
        con.print("")
        con.print("Press Ctrl-C at any time to abort.")
        con.print("")

        # 1. Name — loop until validate_name accepts it.
        try:
            name = self._prompt_name(default=args.name)
            owner = self._prompt_owner(default=args.owner or "b-tree-labs")
            license_id = self._prompt_license(default=args.license or "Apache-2.0")
            default_desc = f"{name} — an AEOS-conformant Axiom extension"
            description = self._prompt_description(
                default=args.description or default_desc
            )
            template = self._prompt_template(explicit=args.template)
        except (KeyboardInterrupt, EOFError):
            con.print("")
            con.print("axi ext init: aborted.")
            return 0

        base_dir = Path(args.target_dir) if args.target_dir else context.cwd
        ext_dir = base_dir / name

        # 6. Preview.
        con.print("")
        table(
            "Review",
            ["field", "value"],
            [
                ["name", name],
                ["owner", owner],
                ["license", license_id],
                ["description", description],
                ["template", template.id],
                ["target path", str(ext_dir)],
            ],
        )
        con.print("")

        # 7. Confirm.
        try:
            proceed = Confirm.ask("Scaffold?", default=True)
        except (KeyboardInterrupt, EOFError):
            proceed = False
        if not proceed:
            con.print("axi ext init: aborted.")
            return 0

        if ext_dir.exists():
            error(
                f"axi ext init: refusing to overwrite existing directory {ext_dir}"
            )
            return 1

        return _scaffold_and_report(
            base_dir,
            template=template,
            name=name,
            owner=owner,
            license=license_id,
            description=description,
        )

    def _prompt_name(self, *, default: str | None) -> str:
        while True:
            raw = Prompt.ask("Extension name", default=default or None)
            if raw is None:
                raw = ""
            name = str(raw).strip()
            err_msg = validate_name(name)
            if err_msg is None:
                return name
            con = console()
            con.print(f"  -> {err_msg}")

    def _prompt_owner(self, *, default: str) -> str:
        con = console()
        con.print("Owner is the organization listed in the manifest.")
        raw = Prompt.ask("Owner", default=default)
        return str(raw).strip() or default

    def _prompt_license(self, *, default: str) -> str:
        con = console()
        con.print(f"Accepted SPDX ids: {allowlist_hint()}")
        while True:
            raw = Prompt.ask("License", default=default)
            resolved = resolve_spdx(str(raw).strip())
            if resolved is not None:
                return resolved
            con.print(f"  -> unknown license {raw!r}; pick one from the list above")

    def _prompt_description(self, *, default: str) -> str:
        raw = Prompt.ask("Description", default=default)
        return str(raw).strip() or default

    def _prompt_template(self, *, explicit: str | None):
        if explicit is not None:
            template = get_template(explicit)
            if template is None:
                error(
                    f"axi ext init: unknown template {explicit!r}",
                    hint="run `axi ext templates` to see registered templates",
                )
                raise KeyboardInterrupt  # abort via common path
            return template
        available = template_registry()
        if len(available) == 1:
            return available[0]
        con = console()
        con.print("Available templates:")
        for t in available:
            con.print(f"  - {t.id}: {t.description}")
        ids = [t.id for t in available]
        default_id = next((t.id for t in available if t.is_default), ids[0])
        while True:
            raw = Prompt.ask("Template", default=default_id)
            chosen = get_template(str(raw).strip())
            if chosen is not None:
                return chosen
            con.print(f"  -> unknown template {raw!r}")


def _scaffold_and_report(
    base_dir: Path,
    *,
    template,
    name: str,
    owner: str,
    license: str,
    description: str,
) -> int:
    """Shared scaffold -> success-report path used by direct + wizard flows."""
    ext_dir = base_dir / name
    if ext_dir.exists():
        error(
            f"axi ext init: refusing to overwrite existing directory {ext_dir}"
        )
        return 1

    description = description or f"{name} — an AEOS-conformant Axiom extension"
    try:
        template.create(
            ext_dir,
            name=name,
            owner=owner,
            license=license,
            description=description,
        )
    except OSError as exc:
        error(f"axi ext init: failed to scaffold {ext_dir}: {exc}")
        return 1

    # Record the scaffold so the graduation-tracking hygiene signal
    # (`check_non_graduated_scaffolds`) can flag prototypes that sit
    # untouched for weeks. Best-effort: a registry write failure does
    # NOT block the scaffold (the user still got a working extension).
    try:
        from axiom.cli.ext.scaffold_registry import record_scaffold
        from axiom.infra.paths import get_project_root
        record_scaffold(get_project_root(), name=name, ext_path=ext_dir)
    except Exception:  # noqa: BLE001 — defensive; never fail init on registry
        pass

    con = console()
    con.print(f"Extension scaffolded at {ext_dir}")
    con.print("")
    next_steps(
        [
            f"cd {ext_dir}",
            "axi ext lint         # Bronze conformance check",
            "axi ext test         # Run the standard tests",
            "axi ext publish --yes # Sign and register locally",
        ]
    )
    return 0


__all__ = ["InitProvider", "validate_name"]
