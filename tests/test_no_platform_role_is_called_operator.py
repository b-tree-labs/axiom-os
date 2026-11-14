# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ROLE-A9: no platform role is called "operator" anywhere a person reads it (ADR-261).

The role is ``site-admin``. The word "operator" stays legitimate for other things: a
maintenance request's ``operator`` field (the person who signed it), ``resolved_by``,
``labelled_by``, an audience in an escalation ladder, a logbook's own signing role, and
English prose about whoever runs a system. So this guard does not ban the word. It looks
for the two shapes that name the ROLE:

1. **Code.** A string literal that is exactly ``"operator"`` in shipped Python. Each one
   left is listed below with the reason it is not the platform role; the list only
   shrinks, and a listed file that no longer has the literal fails the guard.
2. **Prose.** Phrases that name the role in shipped help, docs and specs: "operator
   role", "--role operator", "role operator", a ``roles = ["operator"]`` grant, or the
   role in backticks next to a role word. Files that record the history of the name
   (the ADRs that introduced and renamed it, archived working notes) are exempt.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "axiom"

#: Shipped Python files that still hold the literal ``"operator"``, and why it is not the
#: platform role. Shrink-only.
LITERAL_ALLOWLIST: dict[str, str] = {
    # The alias table itself: the retired id is the key that maps it to site-admin.
    "governance/roles.py": "the retired id in LEGACY_ROLE_ALIASES",
    # Signed maintenance requests carry the signing person's handle in an `operator` field.
    "infra/maintenance.py": "maintenance request field: the person who signed it",
    "extensions/builtins/maintenance/cli.py": "maintenance request field",
    "extensions/builtins/maintenance/skills/__init__.py": "maintenance request field",
    "extensions/builtins/data_platform/ingest_sink/maintenance_box.py": "relay party kind: a listed maintenance signer",
    # Escalation and follow-up ladders address an audience, not a granted role.
    "extensions/builtins/update/escalation.py": "escalation audience (who is contacted next)",
    "extensions/builtins/program/followup_engine.py": "follow-up audience",
    "extensions/builtins/program/followup_model.py": "follow-up audience",
    # Provenance values naming who did something, not what they may do.
    "extensions/builtins/federation/security_cli.py": "resolved_by provenance value",
    "extensions/builtins/data_platform/skills/kit_promotion.py": "fallback login name in an actor handle",
    "extensions/builtins/process/skills/stop.py": "default actor label for an audit record",
    # A release mode, the federation security contact kind, notification sink audience.
    "extensions/builtins/release/mode.py": "release mode: an installed (non-editable) consumer",
    "extensions/builtins/release/_legacy_rivet_cli.py": "notification audience in the legacy CLI",
    "vega/federation/security.py": "security contact kind (operator, backup, coordinator)",
    # Topic keywords for extracting text about dashboards; English, not a grant.
    "extensions/builtins/signals/extractors/calendar.py": "topic keyword",
    "extensions/builtins/signals/extractors/feedback.py": "topic keyword",
    "extensions/builtins/signals/extractors/notes.py": "topic keyword",
    "extensions/builtins/signals/prompts/extraction_v1.py": "topic keyword",
}

#: Phrases that name a platform role "operator".
ROLE_PHRASES = re.compile(
    r"\boperator role|--role operator|\brole operator\b|role[s]?\s*=\s*\[[^\]]*\"operator\""
    r"|`operator`\s*(?:role|\|)|role `operator`|ADR-151's ``?operator",
)

#: Where prose is checked: shipped help, extension docs, and the living docs.
PROSE_GLOBS = ("src/axiom/**/*.py", "src/axiom/**/*.md", "src/axiom/**/*.toml",
               "docs/specs/*.md", "docs/prds/*.md", "docs/guides/**/*.md",
               "docs/reference/**/*.md", "docs/conventions/*.md", "AGENTS.md")

#: Files that record the name's history, or a logbook's own roles (not the platform's).
PROSE_EXEMPT = {
    "src/axiom/governance/roles.py",
    # Logbook signing roles are named by each logbook (ADR-142), not by the platform.
    "src/axiom/extensions/builtins/attest/roles.py",
    "src/axiom/extensions/builtins/attest/log_cli.py",
    "docs/specs/spec-attestation.md",
}


def _is_test(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_") or path.name == "conftest.py"


def _literal_hits() -> dict[str, list[int]]:
    hits: dict[str, list[int]] = {}
    for path in SRC.rglob("*.py"):
        if _is_test(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "operator":
                hits.setdefault(path.relative_to(SRC).as_posix(), []).append(node.lineno)
    return hits


def test_role_a9_no_shipped_code_names_the_operator_role():
    hits = _literal_hits()
    unexplained = {f: lines for f, lines in hits.items() if f not in LITERAL_ALLOWLIST}
    assert not unexplained, (
        "a string literal 'operator' in shipped code: if it is the platform role, it is "
        "'site-admin' now (axiom.governance.roles.SITE_ADMIN); if it is something else, add "
        f"the file to LITERAL_ALLOWLIST with the reason: {unexplained}")


def test_role_a9_the_literal_allowlist_only_shrinks():
    hits = _literal_hits()
    stale = sorted(f for f in LITERAL_ALLOWLIST if f not in hits)
    assert not stale, f"these files no longer hold the literal; delete their allowlist lines: {stale}"


def test_role_a9_no_shipped_prose_names_the_operator_role():
    found: list[str] = []
    for pattern in PROSE_GLOBS:
        for path in ROOT.glob(pattern):
            rel = path.relative_to(ROOT).as_posix()
            if rel in PROSE_EXEMPT or _is_test(path) or not path.is_file():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if ROLE_PHRASES.search(line):
                    found.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not found, "prose names a platform role 'operator'; it is 'site-admin' (Site admin):\n" + "\n".join(found)


def test_role_a9_the_guard_can_fail():
    """Negative control: the phrase and literal detectors fire on the shapes they look for."""
    for text in ('needs the operator role', 'axi gate adduser x --role operator',
                 'roles = ["operator"]', '| `operator` | governs |'):
        assert ROLE_PHRASES.search(text), text
    assert not ROLE_PHRASES.search("the operator who runs the node reads this")
    tree = ast.parse('ROLES = {"operator"}')
    assert any(isinstance(n, ast.Constant) and n.value == "operator" for n in ast.walk(tree))
