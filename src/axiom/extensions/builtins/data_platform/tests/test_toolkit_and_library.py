# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The data-platform toolkit and the library that calls it.

Interpretation (stated because AEOS does not define these two words):

- The latest governing text is ``docs/specs/spec-aeos-0.1.md`` at 0.1.0 plus
  the 0.2 draft amendments in that file (§4.11, §4.12). ``spec-aeos-1.0.md``
  says it is a draft and that 0.1 still governs conformance.
- A toolkit is the extension's callable tool surface. Here that is the tenant
  data kit (``kit/``): the functions ``axi data kit-*`` and ``data.kit_*``
  wrap. It is not an MCP ``kind = "tool"`` — the manifest marks MCP
  not-applicable, and pipelines run on a schedule.
- A library is the importable Python API that helps a caller use that toolkit
  without the CLI. Here that is ``library.py``, which re-exports the kit's
  public names, plus the extension ``__all__`` (AEOS §7.3). The built-in
  relaxation that makes ``__all__`` optional is the partial conformance this
  test refuses.
- Full layout conformance for a built-in means each provided core kind has
  its canonical directory and the manifest entry points into that directory
  (AEOS §5.1). ``axi ext lint`` only warns (AEOS040) when the directory is
  missing; that warning is a gap, not a pass.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[1]
MANIFEST = EXT / "axiom-extension.toml"


def _manifest() -> dict:
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))


def _entry_module(entry: str) -> str:
    return entry.split(":", 1)[0]


def test_public_library_declares_a_non_empty_all() -> None:
    tree = ast.parse((EXT / "__init__.py").read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            assert isinstance(node.value, ast.List)
            names = [elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)]
    assert "library" in names
    assert len(names) >= 2


def test_library_reexports_the_kit_toolkit() -> None:
    from axiom.extensions.builtins.data_platform import library
    from axiom.extensions.builtins.data_platform.kit import __all__ as kit_all

    assert list(library.__all__) == list(kit_all)
    for name in library.__all__:
        assert hasattr(library, name), name
        assert getattr(library, name) is getattr(
            __import__(
                "axiom.extensions.builtins.data_platform.kit",
                fromlist=[name],
            ),
            name,
        )


def test_cmd_and_service_entries_live_in_kind_directories() -> None:
    provides = _manifest()["extension"]["provides"]
    expected_prefix = {
        "cmd": "axiom.extensions.builtins.data_platform.commands.",
        "service": "axiom.extensions.builtins.data_platform.services.",
    }
    seen = set()
    for block in provides:
        kind = block.get("kind")
        if kind not in expected_prefix:
            continue
        seen.add(kind)
        module = _entry_module(block["entry"])
        assert module.startswith(expected_prefix[kind]), block["entry"]
        rel = module.removeprefix("axiom.extensions.builtins.data_platform.")
        path = EXT.joinpath(*rel.split(".")).with_suffix(".py")
        assert path.is_file(), path
    assert seen == {"cmd", "service"}


def test_compatibility_paths_still_resolve_the_implementation() -> None:
    from axiom.extensions.builtins.data_platform import cli, read_mount
    from axiom.extensions.builtins.data_platform.commands import cli as impl
    from axiom.extensions.builtins.data_platform.orchestration import service
    from axiom.extensions.builtins.data_platform.services import orchestrator
    from axiom.extensions.builtins.data_platform.services import read_mount as read_impl

    assert cli.main is impl.main
    assert cli.resolve_skill is impl.resolve_skill
    assert read_mount.mount_spec is read_impl.mount_spec
    assert service.OrchestratorService is orchestrator.OrchestratorService


def test_kind_directories_are_not_empty() -> None:
    for dirname in ("commands", "services", "skills", "agents", "kit"):
        package = EXT / dirname
        assert package.is_dir(), dirname
        sources = [
            path
            for path in package.rglob("*.py")
            if path.name != "__init__.py" and "tests" not in path.parts
        ]
        assert sources, dirname


@pytest.mark.parametrize(
    "module_name",
    [
        "axiom.extensions.builtins.data_platform.commands.cli",
        "axiom.extensions.builtins.data_platform.services.read_mount",
        "axiom.extensions.builtins.data_platform.services.orchestrator",
    ],
)
def test_manifest_entries_import(module_name: str) -> None:
    import importlib

    importlib.import_module(module_name)
