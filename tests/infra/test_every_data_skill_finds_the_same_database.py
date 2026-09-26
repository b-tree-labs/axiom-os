# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Nine skills, nine answers to "which database?".

Each data-platform skill resolved a DSN by hand and they did not agree. On a
node whose environment carries none of those names:

    $ axi db migrate upgrade head
    ✅ Upgrade complete                      <- reached the database
    $ axi data ensure-schema
    ERROR: no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL / AXIOM_DB_URL
    $ axi data backup
    ERROR: no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL

`data.backup` is the one a scheduled nightly cadence invokes. Arming the
schedule against it would have registered a PULSE row that fires at 2am, fails,
and leaves a policy reading `enabled = true` with no backup behind it — the
failure looking exactly like success, which is the thing this whole area keeps
producing.

Fixing `ensure_schema` alone closed one instance and left eight. This closes
the class: a skill may not invent its own precedence.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SKILLS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/axiom/extensions/builtins/data_platform/skills"
)

#: The env names that were being read directly. Reading one of these inside a
#: skill means that skill has its own precedence, which is how they drifted.
FORBIDDEN_ENV = {"DP1_RAG_DSN", "DATABASE_URL", "AXIOM_DB_URL"}


def _reads_dsn_env_directly(path: Path) -> list[tuple[int, str]]:
    """``(lineno, name)`` for each ``os.environ.get("<a DSN name>")``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        if not isinstance(f, ast.Attribute) or f.attr not in {"get", "__getitem__"}:
            continue
        # os.environ.get(...) / environ.get(...)
        base = f.value
        base_name = getattr(base, "attr", None) or getattr(base, "id", None)
        if base_name != "environ":
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and arg.value in FORBIDDEN_ENV:
            found.append((node.lineno, arg.value))
    return found


def _skill_files() -> list[Path]:
    return sorted(p for p in SKILLS_DIR.glob("*.py") if p.name != "__init__.py")


def test_there_are_skills_to_check():
    """A guard over an empty set passes forever and proves nothing."""
    assert len(_skill_files()) >= 10, "skill discovery is broken"


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.name)
def test_no_skill_invents_its_own_dsn_precedence(path: Path):
    offences = _reads_dsn_env_directly(path)
    assert not offences, (
        f"{path.name}: "
        + "; ".join(f"line {n}: os.environ.get({v!r})" for n, v in offences)
        + " — use data_platform._dsn.resolve_dsn, which ends at "
        "axiom.infra.db.platform_db_url. A skill that cannot find the database "
        "the process is already talking to fails on a node where every other "
        "door works."
    )


def test_the_resolver_ends_at_the_platforms_own_url(monkeypatch):
    from axiom.extensions.builtins.data_platform._dsn import resolve_dsn
    from axiom.infra.db import platform_db_url

    for name in FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)
    assert resolve_dsn({}) == platform_db_url()
    assert resolve_dsn(None) == platform_db_url()


def test_precedence_explicit_then_env_then_platform(monkeypatch):
    from axiom.extensions.builtins.data_platform._dsn import resolve_dsn

    for name in FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/env")
    assert resolve_dsn({}) == "postgresql://u@h/env"
    assert resolve_dsn({"dsn": "postgresql://u@h/explicit"}) == "postgresql://u@h/explicit"

    monkeypatch.setenv("DP1_RAG_DSN", "postgresql://u@h/rag")
    assert resolve_dsn({}) == "postgresql://u@h/rag", "DP1_RAG_DSN is read first"


def test_a_connector_specific_env_name_wins_over_the_generic_ones(monkeypatch):
    """`reindex` and friends carry a configurable env name in their config."""
    from axiom.extensions.builtins.data_platform._dsn import resolve_dsn

    monkeypatch.setenv("DP1_RAG_DSN", "postgresql://u@h/generic")
    monkeypatch.setenv("MY_CORPUS_DSN", "postgresql://u@h/specific")
    assert resolve_dsn({}, env_name="MY_CORPUS_DSN") == "postgresql://u@h/specific"


def test_the_check_can_actually_fail(tmp_path):
    """Negative control."""
    bad = tmp_path / "bad.py"
    bad.write_text("import os\ndsn = os.environ.get('DP1_RAG_DSN')\n")
    assert _reads_dsn_env_directly(bad) == [(2, "DP1_RAG_DSN")]

    good = tmp_path / "good.py"
    good.write_text("from .._dsn import resolve_dsn\ndsn = resolve_dsn({})\n")
    assert _reads_dsn_env_directly(good) == []

    unrelated = tmp_path / "other.py"
    unrelated.write_text("import os\nx = os.environ.get('HOME')\n")
    assert _reads_dsn_env_directly(unrelated) == []
