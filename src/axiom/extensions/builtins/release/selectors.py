# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who decides which tests a change affects.

RIVET does not keep its own map from paths to tests. A ``TestSelector``
answers that. The default asks pytest-testmon, through its database and
``pytest --testmon``. When the map is missing, stale, or the change
touches setup or dependencies, or the run is the merge queue, main, or
a nightly, the answer is the whole suite. A repository that is not
Python uses ``NullSelector``, which always runs the whole suite.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from axiom.extensions.builtins.release.lander.protect import DEPENDENCY_PATTERNS

_SETUP_DEFAULTS = (
    r"(^|/)conftest\.py$",
    r"(^|/)pytest\.ini$",
    r"(^|/)tox\.ini$",
    r"(^|/)noxfile\.py$",
    r"(^|/)\.github/workflows/[^/]+$",
    r"(^|/)\.gitlab-ci\.yml$",
)
_DEPENDENCY_DEFAULTS = DEPENDENCY_PATTERNS
_FULL_CONTEXTS = frozenset({"merge-queue", "main", "nightly"})


@dataclass(frozen=True)
class Selection:
    """The tests to run, or the whole suite, and why."""

    run_all: bool
    tests: tuple[str, ...]
    reason: str
    fraction: float = 1.0

    def __post_init__(self) -> None:
        if self.run_all and self.fraction != 1.0:
            object.__setattr__(self, "fraction", 1.0)
        if not self.run_all:
            object.__setattr__(self, "fraction", min(1.0, max(0.0, self.fraction)))


class TestSelector(Protocol):
    """``select(changed_paths, base_sha, head_sha) -> Selection``."""

    def select(
        self,
        changed_paths: Sequence[str],
        base_sha: str,
        head_sha: str,
    ) -> Selection:
        """Tests affected by this change, or the whole suite."""


class NullSelector:
    """No language map. Every change runs the whole suite."""

    name = "null"

    def select(
        self,
        changed_paths: Sequence[str],
        base_sha: str,
        head_sha: str,
    ) -> Selection:
        del changed_paths, base_sha, head_sha
        return Selection(True, (), "no-language-selector")


def context_from_environment(environ: dict[str, str] | None = None) -> str:
    """pull_request, main, merge-queue, or nightly. Nothing here names a repo."""
    env = os.environ if environ is None else environ
    event = env.get("GITHUB_EVENT_NAME", "") or env.get("CI_PIPELINE_SOURCE", "")
    ref = env.get("GITHUB_REF", "") or env.get("CI_COMMIT_REF_NAME", "")
    if event == "merge_group" or env.get("CI_MERGE_REQUEST_EVENT_TYPE") == "merge_train":
        return "merge-queue"
    if event == "schedule":
        return "nightly"
    leaf = ref.rsplit("/", 1)[-1]
    if leaf in {"main", "master"}:
        return "main"
    return "pull_request"


def _compiled(patterns: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


def _hits(path: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(pattern.search(path) for pattern in patterns)


def scope_id(tests: Sequence[str]) -> str:
    """Stable id for one testmon answer. Empty when there is nothing to share."""
    names = tuple(tests)
    if not names:
        return ""
    digest = hashlib.sha1("\n".join(names).encode()).hexdigest()[:12]
    return f"tests:{digest}"


class TestmonSelector:
    """Delegate to pytest-testmon. Fall back to the whole suite when it cannot answer."""

    __test__ = False
    name = "testmon"

    def __init__(
        self,
        database: Path | str = ".testmondata",
        *,
        context: str | None = None,
        test_setup: Sequence[str] = _SETUP_DEFAULTS,
        dependencies: Sequence[str] = _DEPENDENCY_DEFAULTS,
        full_contexts: Sequence[str] = tuple(_FULL_CONTEXTS),
    ) -> None:
        self.database = Path(database)
        self.context = context
        self._setup = _compiled(test_setup)
        self._dependencies = _compiled(dependencies)
        self._full = frozenset(full_contexts)

    def select(
        self,
        changed_paths: Sequence[str],
        base_sha: str,
        head_sha: str,
    ) -> Selection:
        del head_sha
        context = self.context if self.context is not None else context_from_environment()
        if context in self._full:
            return Selection(True, (), context)
        paths = tuple(changed_paths)
        if any(_hits(path, self._setup) for path in paths):
            return Selection(True, (), "test-setup")
        if any(_hits(path, self._dependencies) for path in paths):
            return Selection(True, (), "dependencies")
        if not self.database.is_file():
            return Selection(True, (), "missing-db")
        try:
            return self._from_database(paths, base_sha)
        except Exception:
            return Selection(True, (), "stale-db")

    def _from_database(self, paths: Sequence[str], base_sha: str) -> Selection:
        """Read testmon's database. Never create it and never delete it."""
        from testmon.db import DATA_VERSION, connect

        connection = connect(str(self.database), readonly=True)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != DATA_VERSION:
                return Selection(True, (), "stale-db")
            if base_sha:
                row = connection.execute(
                    "SELECT data FROM metadata WHERE dataid = ?",
                    ("base_sha",),
                ).fetchone()
                if row is not None and str(row[0]) != base_sha:
                    return Selection(True, (), "stale-db")
            known = {
                str(item[0])
                for item in connection.execute("SELECT DISTINCT filename FROM file_fp")
            }
            if not known and paths:
                return Selection(True, (), "missing-db")
            for path in paths:
                if path.endswith(".py") and path not in known:
                    return Selection(True, (), "unmapped")
            if not paths:
                return Selection(False, (), "unaffected", 0.0)
            placeholders = ",".join("?" for _ in paths)
            affected_rows = connection.execute(
                f"""
                SELECT DISTINCT te.test_name, te.duration
                FROM test_execution te
                JOIN test_execution_file_fp link ON te.id = link.test_execution_id
                JOIN file_fp file ON link.fingerprint_id = file.id
                WHERE file.filename IN ({placeholders})
                """,
                tuple(paths),
            ).fetchall()
            totals = connection.execute(
                "SELECT test_name, duration FROM test_execution"
            ).fetchall()
        finally:
            connection.close()
        tests = tuple(sorted({str(row[0]) for row in affected_rows}))
        if not tests:
            return Selection(False, (), "unaffected", 0.0)
        affected_time = sum(float(row[1] or 0) for row in affected_rows)
        total_time = sum(float(row[1] or 0) for row in totals)
        if total_time > 0:
            fraction = affected_time / total_time
        elif totals:
            fraction = len(tests) / len(totals)
        else:
            fraction = 1.0
        return Selection(False, tests, "testmon", fraction)


def resolve_selector(name: str = "testmon", **kwargs: object) -> TestSelector:
    """Built-ins first, then the ``axiom.release.test_selector`` entry-point group."""
    if name == "null":
        return NullSelector()
    if name in {"", "testmon"}:
        return TestmonSelector(**kwargs)  # type: ignore[arg-type]
    try:
        from importlib.metadata import entry_points

        found = entry_points(group="axiom.release.test_selector")
        for item in found:
            if item.name == name:
                loaded = item.load()
                return loaded(**kwargs) if kwargs else loaded()
    except Exception:
        pass
    raise LookupError(f"no test selector named {name!r}")


def suite_minutes_for(selection: Selection, suite_minutes: int) -> int:
    """What one change costs, using the selector's share of the suite."""
    if selection.run_all:
        return suite_minutes
    return int(round(suite_minutes * selection.fraction))
