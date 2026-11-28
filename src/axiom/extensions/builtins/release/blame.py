# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which failing tests a merge can be blamed for (#2037).

Auto-revert once reverted a merge for two flaky tests the merge did not touch. After the
failed jobs are re-run once, the tests that still fail are judged here, from the failing test
ids and the paths the merge changed. A test is **blamed** when it touches a changed path:

1. its own file was changed;
2. the repository's test selector selects it for the changed paths;
3. it imports a changed module (a direct import; a transitive one is not followed);
4. it sits in the tests of a package the merge changed (``<package>/tests/``), or a changed
   ``conftest.py`` applies to it;
5. its name matches a changed module (``test_widget.py`` and ``widget.py``);
6. its own failure text names a file the merge changed (a guard that scans many files lists the
   offenders it found).

A guard that scans many files is **not** blamed for scanning: a repository-wide scan reads every file,
so "it scans a file this merge added" would blame every merge for every scan failure, and the revert
would land on whichever commit happened to be on main when the guard went red. Only the files the
failure itself names count (rule 6), matched on a directory boundary (``agreement/cli.py`` matches
``src/pkg/agreement/cli.py``, never ``xcli.py``). A scan failure that names no file blames nobody: when
unsure, nothing is reverted and a person is told which tests were not blamed.

Anything else is **cleared**, with the reason. The function reads nothing itself: the source
of a test file and the selector are handed in, so it is a pure decision with unit tests. A
revert needs at least one blamed test; with none, a person decides and is told which tests
were and were not blamed, and why.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class TestVerdict:
    __test__ = False
    test: str
    why: str

    def to_dict(self) -> dict:
        return {"test": self.test, "why": self.why}


@dataclass(frozen=True)
class Blame:
    blamed: tuple[TestVerdict, ...]
    cleared: tuple[TestVerdict, ...]


def test_file(test_id: str) -> str:
    return test_id.split("::", 1)[0]


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".py")


def _dotted(path: str) -> str:
    """``src/pkg/mod.py`` as ``pkg.mod``; a package's ``__init__.py`` as the package."""
    name = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return name.removesuffix(".__init__")


def _imports(source: str, changed_py: str) -> bool:
    dotted = _dotted(changed_py)
    if not dotted:
        return False
    escaped = re.escape(dotted)
    if re.search(rf"^\s*(?:from|import)\s+{escaped}\b", source, re.MULTILINE):
        return True
    if "." in dotted:
        parent, stem = dotted.rsplit(".", 1)
        return bool(
            re.search(rf"^\s*from\s+{re.escape(parent)}\s+import\s+[^\n]*\b{re.escape(stem)}\b", source, re.MULTILINE)
        )
    return False


def _why(
    test: str,
    changed: Sequence[str],
    *,
    read_source: Callable[[str], str | None] | None,
    selected: tuple[str, ...] | None,
    named: Sequence[str] = (),
) -> str:
    """Why ``test`` touches the change, or an empty string when it does not."""
    file = test_file(test)
    if file in changed:
        return f"its own file was changed by the merge ({file})"
    if selected is not None and (test in selected or file in selected):
        return "the repository's test selector selects it for the changed paths"
    source = None
    if read_source is not None:
        try:
            source = read_source(file)
        except Exception:  # noqa: BLE001 - an unreadable file is "does not import", never a blame
            source = None
    py_changes = [path for path in changed if path.endswith(".py")]
    if source:
        for path in py_changes:
            if _imports(source, path):
                return f"it imports {_dotted(path)}, which the merge changed"
    if "/tests/" in f"/{file}":
        package = f"/{file}".split("/tests/", 1)[0].lstrip("/")
        if package:
            for path in changed:
                if path.startswith(package + "/"):
                    return f"it sits in the tests of {package}, which the merge changed ({path})"
    directory = file.rsplit("/", 1)[0] if "/" in file else ""
    for path in changed:
        if path.rsplit("/", 1)[-1] == "conftest.py":
            base = path.rsplit("/", 1)[0] if "/" in path else ""
            if not base or directory == base or directory.startswith(base + "/"):
                return f"a conftest.py the merge changed applies to it ({path})"
    stem = _stem(file).removeprefix("test_").removesuffix("_test")
    for path in py_changes:
        if stem and stem != "__init__" and _stem(path) == stem:
            return f"its name matches the changed module {path}"
    for path in named:
        for changed_path in changed:
            if changed_path == path or changed_path.endswith("/" + path):
                return f"its own failure names {path}, which the merge changed ({changed_path})"
    return ""


def blame_tests(
    tests: Sequence[str],
    changed: Sequence[str],
    *,
    read_source: Callable[[str], str | None] | None = None,
    selector: object | None = None,
    base_sha: str = "",
    head_sha: str = "",
    named: Mapping[str, Sequence[str]] | None = None,
) -> Blame:
    """Split ``tests`` into those the merge can be blamed for and those it cannot."""
    changed = list(changed)
    selected: tuple[str, ...] | None = None
    if selector is not None:
        try:
            chosen = selector.select(changed, base_sha, head_sha)  # type: ignore[attr-defined]
            if not chosen.run_all:
                selected = tuple(chosen.tests)
        except Exception:  # noqa: BLE001 - a selector that cannot answer neither blames nor clears
            selected = None
    blamed: list[TestVerdict] = []
    cleared: list[TestVerdict] = []
    shown = ", ".join(changed[:4]) + (f" and {len(changed) - 4} more" if len(changed) > 4 else "")
    for test in tests:
        why = _why(test, changed, read_source=read_source, selected=selected, named=(named or {}).get(test, ()))
        if why:
            blamed.append(TestVerdict(test, why))
        else:
            cleared.append(
                TestVerdict(
                    test,
                    "it does not touch what the merge changed: not its file, a module it imports, "
                    f"its package, a matching name or the selector's choice ({shown or 'no files read'})",
                )
            )
    return Blame(tuple(blamed), tuple(cleared))
