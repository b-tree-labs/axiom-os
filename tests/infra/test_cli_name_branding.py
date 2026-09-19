# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Printed commands name the CLI the operator is actually running.

``axi`` is the platform CLI name; a consumer distribution rebrands it (say a
product whose CLI is ``neut``). Hundreds of user-facing strings hardcoded the
literal ``"axi "``, so an operator running ``neut ext config`` was told
``axi ext config: missing operation``, ``neut log --help`` printed
``usage: axi log``, and every remediation line ("Run ``axi db up``") pointed
at a command that does not exist on their machine.

Behaviour under test:

- printed error prefixes, argparse ``prog=`` and ``--help`` epilog examples
  all resolve the command name from the *active* branding, so they read
  "acme" under Acme branding and "axi" with no branding registered;
- a source guard: no string literal that reaches the terminal (``print`` and
  friends, or an argparse ``prog``/``help``/``description``/``epilog``) may
  hardcode ``axi <verb>`` again, apart from the handful of sites in
  :data:`HARDCODED_AXI_EXEMPT` where "axi" is genuinely part of a fixed
  identifier (the ``~/.local/bin/axi`` shim, the ``--axi`` flag) rather than
  the name of a command to type.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

import pytest

from axiom.infra import branding

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "axiom"

#: `axi ` followed by a lowercase word — i.e. a command, not `axiom`,
#: `axi-platform`, `~/.axi` or `AXI_STATE_DIR`.
COMMAND_MENTION = re.compile(r"(?<![A-Za-z0-9_.\-/])axi (?=[a-z])")

PRINTERS = {
    "print", "error", "write", "info", "warn", "warning", "success", "text",
    "heading", "numbered_steps", "next_steps", "emit_error", "emit_info",
    "emit_next_steps", "_error", "_warn",
}
ARGPARSE_TEXT = {"prog", "help", "description", "epilog", "usage"}

#: (path relative to src/axiom, exact literal) pairs where the hardcoded
#: "axi" is deliberate. Each names a fixed identifier, not a command:
HARDCODED_AXI_EXEMPT: set[tuple[str, str]] = {
    # Printed only when reading the branding itself raised, so there is no
    # brand to report; "axi" is the last-resort fallback.
    ("axiom_cli.py", "axi unknown"),
    # `--axi` is the flag's own name, and the value is a path to an
    # executable that may well be literally named `axi`.
    ("extensions/builtins/memory/cli.py",
     "axi binary the hook should invoke (default: the pinned service venv)."),
    ("extensions/builtins/memory/cli.py", "  axi binary: "),
    # `axi install-shim` writes ~/.local/bin/axi. The shim's filename does
    # not change with branding, so neither does the prose about it.
    ("extensions/builtins/install/shim_cli.py",
     "Install a stable ~/.local/bin/axi shim so non-interactive SSH "
     "sessions (federation peers) can locate axi without walking the "
     "filesystem."),
    ("extensions/builtins/install/shim_cli.py",
     "Explicit path to the venv-installed axi binary "
     "(default: auto-detect from sys.argv[0] or $PATH)."),
    ("extensions/builtins/install/shim_cli.py",
     "  error: could not locate the current axi binary. "
     "Pass --target /path/to/.venv/bin/axi explicitly."),
    ("extensions/builtins/install/shim_cli.py",
     "  Multiple axi installs are competing for the same shim."),
    # "axi configuration" names the product's configuration, not a command.
    ("extensions/builtins/settings/cli.py", "View and edit axi configuration"),
    # "an axi command" — branding the article too is a separate change.
    ("setup/wizard.py",
     "They load automatically every time you run an axi command."),
}


@pytest.fixture
def acme_branding():
    """Run the body under a consumer distribution whose CLI is `acme`."""
    branding.register(branding.BrandingConfig(cli_name="acme", product_name="Acme"))
    try:
        yield
    finally:
        branding.reset()


def test_printed_error_prefix_follows_branding(acme_branding, capsys, tmp_path):
    """`neut ext config` used to answer "axi ext config: ...", naming a CLI
    the operator does not have installed."""
    from axiom.cli.ext.commands.config import ConfigProvider
    from axiom.cli.ext.provider import CliContext

    ConfigProvider().run(
        argparse.Namespace(args=[], json=False),
        CliContext(cwd=tmp_path),
    )
    printed = capsys.readouterr()
    assert "acme ext config:" in printed.out + printed.err
    assert "axi ext config:" not in printed.out + printed.err


def test_argparse_prog_and_epilog_follow_branding(acme_branding):
    """`--help` is the operator's first contact with the CLI; it used to
    print `usage: axi log` and a block of `axi log tail ...` examples no
    matter which distribution was running."""
    from axiom.extensions.builtins.log.cli import get_parser

    parser = get_parser()
    assert parser.prog == "acme log"
    assert "acme log tail" in (parser.epilog or "")
    assert "axi log" not in (parser.epilog or "")


def test_platform_default_is_still_axi():
    """With no branding registered the platform must still call itself axi —
    the fix must not leave the default distribution nameless."""
    branding.reset()
    from axiom.extensions.builtins.log.cli import get_parser

    assert get_parser().prog == "axi log"


def _terminal_bound_literals(path: Path):
    """Yield string constants in `path` that get printed or shown in --help."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def reaches_terminal(node: ast.AST) -> bool:
        cur = node
        for _ in range(8):
            parent = parents.get(id(cur))
            if parent is None:
                return False
            if isinstance(parent, ast.keyword) and parent.arg in ARGPARSE_TEXT:
                return True
            if isinstance(parent, ast.Call):
                func = parent.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else func.id if isinstance(func, ast.Name) else "")
                return name in PRINTERS
            cur = parent
        return False

    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings
                and COMMAND_MENTION.search(node.value)
                and reaches_terminal(node)):
            yield node.lineno, node.value


def test_no_new_hardcoded_axi_in_operator_facing_text():
    """The regression this guards: someone adds `print("axi foo: ...")` and
    the consumer distribution starts advertising a command that is not on
    the operator's PATH again. Route it through the active branding's
    `cli_name` instead (see `_brand_cli()` in any CLI module)."""
    offenders: list[str] = []
    seen_exempt: set[tuple[str, str]] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        if "/tests/" in f"/{rel}" or path.name.startswith("test_"):
            continue
        for lineno, value in _terminal_bound_literals(path):
            if (rel, value) in HARDCODED_AXI_EXEMPT:
                seen_exempt.add((rel, value))
                continue
            offenders.append(f"{rel}:{lineno}: {value!r}")
    assert not offenders, (
        "operator-facing text hardcodes the platform CLI name:\n  "
        + "\n  ".join(offenders)
    )
    stale = HARDCODED_AXI_EXEMPT - seen_exempt
    assert not stale, f"stale exemptions — the sites are gone, drop them: {stale}"
