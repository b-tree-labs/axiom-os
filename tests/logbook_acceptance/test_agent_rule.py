# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Software proposes and a person signs (ADR-142). This guard keeps it so.

Chat and MCP reach skills through the `agent_tool` and `mcp` surfaces, so the whole safety
argument is that no skill able to sign is on either surface. This is true today, so these
are normal tests, not expected failures. A unit that adds a signing skill adds its name to
`SIGNING_SKILLS`. A unit that adds a skill which only proposes keeps it in a module that
never calls `service.sign`, never imports the library's `axiom.attest.sign` and never calls
`confirm.respond` (F1b): the library is not a boundary against code in the node's process,
so the surfaces are.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect

from axiom.extensions.builtins.attest.skills import SPECS
from axiom.infra.orchestrator import skills as approval_skills

AGENT_SURFACES = {"mcp", "agent_tool"}

#: Skills that sign, or that decide for a person. The later ones arrive with L2 to L5.
SIGNING_SKILLS = {
    "attest.new",
    "attest.sign",
    "attest.supplement",
    "attest.ack",
    "attest.cosign",
    "attest.seal",
    "approval.approve",
    "approval.reject",
}


class _Collect:
    """Stands in for a registry, to read the approval skills' declarations."""

    def __init__(self):
        self.specs = []

    def register_skill(self, spec, **_):
        self.specs.append(spec)


_approval = _Collect()
approval_skills.register_all(_approval)
ALL_SKILLS = (*SPECS, *_approval.specs)


def _calls_service_sign(module) -> bool:
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if node.func.attr == "sign" and isinstance(base, ast.Name) and base.id == "service":
                return True
    return False


#: Modules that sign, or answer for a person, by their absolute names.
_LIBRARY = "axiom.attest"
_SERVICE = "axiom.extensions.builtins.attest.service"
_SIGNING_SKILLS_MODULE = "axiom.extensions.builtins.attest.skills.sign"


def _absolute(node: ast.ImportFrom, package: str | None) -> str | None:
    """The absolute module an ``ImportFrom`` names; None for a relative one with no package."""
    if not node.level:
        return node.module
    if not package:
        return None
    try:
        return importlib.util.resolve_name("." * node.level + (node.module or ""), package)
    except (ImportError, ValueError):
        return None


def _signs(module: str, names: set[str]) -> bool:
    """Importing ``names`` from ``module`` reaches a signature or a person's answer."""
    if module in (f"{_LIBRARY}.sign", _SIGNING_SKILLS_MODULE):
        return True
    if module.startswith((f"{_LIBRARY}.sign.", f"{_SIGNING_SKILLS_MODULE}.")):
        return True
    if module == _LIBRARY and "sign" in names:
        return True
    if module == f"{_LIBRARY}.confirm" and "respond" in names:
        return True
    if module == _SERVICE and names & {"sign", "respond"}:
        return True
    if module == "axiom.extensions.builtins.attest.skills" and "sign" in names:
        return True
    return False


def _reaches_library_signing(source: str, package: str | None = None) -> bool:
    """The module imports `axiom.attest.sign`, or answers for a person with `confirm.respond`.

    ``package`` is the module's own package, so a relative import (``from .sign import sign``
    inside `axiom.attest`, ``from ..service import sign`` inside the attest skills) resolves to
    the module it names. With no package, a relative import is judged by its last name alone,
    which over-reports rather than misses."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(
                a.name == f"{_LIBRARY}.sign" or a.name.startswith(f"{_LIBRARY}.sign.")
                for a in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            module = _absolute(node, package)
            if module is not None:
                if _signs(module, names):
                    return True
            else:  # a relative import with nothing to resolve it against
                tail = (node.module or "").rsplit(".", 1)[-1]
                if tail == "sign" or "sign" in names:
                    return True
                if tail in ("confirm", "service") and "respond" in names:
                    return True
        elif isinstance(node, ast.Attribute) and node.attr == "respond":
            base = node.value
            if (isinstance(base, ast.Name) and base.id == "confirm") or (
                isinstance(base, ast.Attribute) and base.attr == "confirm"
            ):
                return True
    return False


def test_no_signing_skill_is_on_an_agent_surface():
    exposed = [
        s.name
        for s in ALL_SKILLS
        if s.name in SIGNING_SKILLS and AGENT_SURFACES & set(s.surfaces or ())
    ]
    assert exposed == []


def test_the_guard_is_not_empty():
    """A guard over a list that matches nothing is green for the wrong reason."""
    present = {s.name for s in ALL_SKILLS} & SIGNING_SKILLS
    assert {"attest.new", "attest.sign"} <= present


def test_a_skill_that_reaches_the_signature_is_on_neither_surface():
    """Checked from the code, so a signing skill with a new name is caught too."""
    sign_module = inspect.getmodule(next(s.fn for s in SPECS if s.name == "attest.sign"))
    assert _calls_service_sign(sign_module)
    for spec in SPECS:
        if AGENT_SURFACES & set(spec.surfaces or ()):
            assert not _calls_service_sign(inspect.getmodule(spec.fn)), spec.name


def test_the_library_guard_catches_each_way_in():
    """A guard that matches nothing is green for the wrong reason, so each form is tried."""
    caught = (
        "from axiom.attest import sign",
        "from axiom.attest import drafts, sign as s",
        "import axiom.attest.sign",
        "import axiom.attest.sign as s",
        "from axiom.attest.sign import sign",
        "from axiom.attest.confirm import respond",
        "from axiom.attest import confirm\nconfirm.respond('p', principal=p, answer='sign', via='cli')",
        "import axiom.attest.confirm\naxiom.attest.confirm.respond('p')",
    )
    for source in caught:
        assert _reaches_library_signing(source), source
    allowed = (
        "from axiom.attest import drafts, query, verify",
        "from axiom.attest import confirm\nconfirm.present('d', modalities=['screen'])",
        "from axiom.attest.confirm import present",
    )
    for source in allowed:
        assert not _reaches_library_signing(source), source


ATTEST_SKILLS = "axiom.extensions.builtins.attest.skills"


def test_the_library_guard_catches_each_relative_way_in():
    """A skill on the `mcp` or `agent_tool` surface reaches signing by a relative import just as
    well, so each relative form is tried in the package it would sit in, and without one."""
    caught = (
        ("from ..attest import sign", "axiom.extensions"),
        ("from ...attest import sign", "axiom.attest.tools"),
        ("from . import sign", "axiom.attest"),
        ("from .sign import sign", "axiom.attest"),
        ("from .sign import sign as s", "axiom.attest"),
        ("from .confirm import respond", "axiom.attest"),
        ("from . import confirm\nconfirm.respond('p')", "axiom.attest"),
        ("from ..service import sign", ATTEST_SKILLS),
        ("from ..service import respond", ATTEST_SKILLS),
        ("from .sign import new", ATTEST_SKILLS),
        ("from . import sign", ATTEST_SKILLS),
        ("from ..attest import sign", None),
        ("from .sign import sign", None),
        ("from .confirm import respond", None),
    )
    for source, package in caught:
        assert _reaches_library_signing(source, package), (source, package)
    allowed = (
        ("from . import drafts, query", "axiom.attest"),
        ("from .confirm import present", "axiom.attest"),
        ("from .. import service", ATTEST_SKILLS),
        ("from ..service import AttestRefused", ATTEST_SKILLS),
        ("from ._common import fail", ATTEST_SKILLS),
        ("from .._common import fail", None),
    )
    for source, package in allowed:
        assert not _reaches_library_signing(source, package), (source, package)


def test_no_skill_on_an_agent_surface_reaches_the_librarys_signature():
    """F1b: importing `axiom.attest.sign` or calling `confirm.respond` is signing, by another door."""
    for spec in ALL_SKILLS:
        if AGENT_SURFACES & set(spec.surfaces or ()):
            module = inspect.getmodule(spec.fn)
            source = inspect.getsource(module)
            assert not _reaches_library_signing(source, module.__package__), spec.name
