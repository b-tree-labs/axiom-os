# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every test file in the repository runs in CI, or says why it does not.

CI's unit job runs a bare ``pytest``, so what it collects is exactly the
``testpaths`` in pyproject.toml. A test file outside those paths is never
collected: it cannot fail, so it proves nothing, and the workstreams that cite
it are citing a proof that never ran. That is how 114 files (1248 tests) under
``src/axiom`` sat outside CI until C-84 (#1230), and how one of them kept
asserting behaviour that ADR-174 had deliberately removed.

This guard fails when a test file sits outside every collection path and is not
listed in ``EXCLUDED`` with a reason and a tracking issue. It also fails when the
unit job stops being a bare ``pytest`` (explicit paths there would override
``testpaths``), or when an exclusion outlives the files it excuses.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

#: Directories never searched: environments, caches, vendored trees.
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "build", "dist"}

#: Files that match pytest's default pattern but are not test modules. Each says what it is.
NOT_TEST_MODULES: dict[str, str] = {
    "src/axiom/cli/ext/commands/test_verb.py": (
        "the `axi ext test` command module; it defines no tests"
    ),
}

#: Test files deliberately outside CI's collection, by path prefix: (reason, tracking issue).
#: An entry is temporary by construction. It must name an open issue, and it fails
#: once no test file sits under its prefix.
EXCLUDED: dict[str, tuple[str, str]] = {}

#: Suites that must run from their own directory (their own rootdir and pytest
#: config), each by a named step in the CI unit job: {directory: step name}.
OWN_ROOT_SUITES: dict[str, str] = {
    # A pytest plugin package: its conftest declares pytest_plugins, which
    # pytest accepts only at the rootdir.
    "packages/axiom-tests": "Run axiom-tests package tests",
}

_ISSUE_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/issues/\d+$")


def _pytest_ini() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]


def _is_test_file(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _all_test_files() -> list[str]:
    found: list[str] = []
    stack = [ROOT]
    while stack:
        directory = stack.pop()
        for entry in directory.iterdir():
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS and not entry.name.startswith(".venv"):
                    stack.append(entry)
            elif _is_test_file(entry.name):
                found.append(entry.relative_to(ROOT).as_posix())
    return sorted(found)


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/") + "/"
    return path.startswith(prefix)


def test_collection_uses_pytest_default_file_pattern():
    """The file walk below mirrors pytest's default ``python_files``; an override would desync it."""
    assert "python_files" not in _pytest_ini(), (
        "pyproject sets python_files; teach this guard the new pattern before changing it"
    )


def _workflow_documents() -> list[tuple[str, dict]]:
    directory = ROOT / ".github" / "workflows"
    return [
        (path.name, yaml.safe_load(path.read_text()))
        for path in sorted(directory.glob("*.yml"))
    ]


def test_ci_unit_job_collects_exactly_the_testpaths():
    """A bare ``pytest`` collects ``testpaths``; explicit paths or --ignore would narrow it silently."""
    steps = [
        step
        for _name, workflow in _workflow_documents()
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == "Run unit tests"
    ]
    assert steps, "no workflow has a 'Run unit tests' step; this guard must follow it"
    for step in steps:
        argv = shlex.split(step["run"])
        pytest_at = next(i for i, arg in enumerate(argv) if arg.endswith("pytest"))
        args = argv[pytest_at + 1 :]
        positional: list[str] = []
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in {"-n", "-m", "-k", "-p", "--tb", "--junitxml", "--timeout"}:
                skip_next = True
            elif not arg.startswith("-"):
                positional.append(arg)
        assert not positional, f"the unit job names paths {positional}; that overrides testpaths"
        narrowing = [a for a in args if a.startswith(("--ignore", "--deselect", "-k", "--override-ini"))]
        assert not narrowing, f"the unit job narrows collection with {narrowing}"
    addopts = _pytest_ini().get("addopts", "")
    assert "--ignore" not in addopts and "--deselect" not in addopts


#: Installs every job needs when it runs a bare root ``pytest``: such a run collects
#: every testpath, so a suite whose imports need an extra fails collection without it.
COLLECTION_INSTALLS: tuple[str, ...] = ('"packages/axiom-ext-data-platform[duckdb]"',)


def _positional_args(argv: list[str]) -> list[str]:
    positional: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in {"-n", "-m", "-k", "-p", "--tb", "--junitxml", "--timeout"}:
            skip_next = True
        elif not arg.startswith("-"):
            positional.append(arg)
    return positional


def test_every_job_that_collects_the_testpaths_installs_what_they_import():
    """Main's Integration job went red once the packages joined testpaths: the unit job
    installed the DuckDB extra and the Integration job, which collects the same paths
    under another marker, did not. A skip there would be the same silent gap. The
    nightly compatibility workflow collects the same paths and is held to the same rule."""
    found_unit = False
    for filename, workflow in _workflow_documents():
        collecting_jobs = []
        for job_name, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                run = step.get("run", "")
                if "pytest" not in run or step.get("working-directory"):
                    continue
                for line in run.replace("\\\n", " ").splitlines():
                    if "pytest" not in line:
                        continue
                    argv = shlex.split(line.split("|")[0])
                    at = next((i for i, a in enumerate(argv) if a.endswith("/pytest") or a == "pytest"), None)
                    # `pip install pytest` names the package; it does not collect.
                    if at is None or any("=" not in token for token in argv[:at]):
                        continue
                    if not _positional_args(argv[at + 1 :]):
                        collecting_jobs.append(job_name)
        if "unit-tests" in collecting_jobs:
            found_unit = True
        for job_name in set(collecting_jobs):
            installs = "\n".join(step.get("run", "") for step in workflow["jobs"][job_name]["steps"])
            missing = [pkg for pkg in COLLECTION_INSTALLS if pkg not in installs]
            assert not missing, (
                f"{filename} job {job_name!r} collects every testpath but does not install {missing}"
            )
    assert found_unit, "found no unit job collecting the testpaths"


def _own_root_testpaths() -> list[str]:
    paths: list[str] = []
    for directory in OWN_ROOT_SUITES:
        config = tomllib.loads((ROOT / directory / "pyproject.toml").read_text())
        ini = config["tool"]["pytest"]["ini_options"]
        paths += [f"{directory}/{tp}" for tp in ini["testpaths"]]
    return paths


def test_every_own_root_suite_has_its_ci_step():
    """A suite that runs from its own directory is collected only if every unit run has a step for it.

    The nightly compatibility workflow runs the same unit step, so it has to run the
    own-root suite too. A 3.11 failure there would otherwise miss the package.
    """
    unit_workflows = []
    for filename, workflow in _workflow_documents():
        steps = {
            step.get("name"): step
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
        }
        if "Run unit tests" in steps:
            unit_workflows.append((filename, steps))
    assert unit_workflows, "no workflow runs the unit step"
    for filename, steps in unit_workflows:
        for directory, step_name in OWN_ROOT_SUITES.items():
            step = steps.get(step_name)
            assert step, f"{filename} has no step {step_name!r} to run {directory}"
            assert step.get("working-directory") == directory, f"{filename} {step_name} does not run in {directory}"
            argv = shlex.split(step["run"])
            assert argv[0].endswith("pytest"), f"{step_name} is not a pytest run"
            positional = [a for a in argv[1:] if not a.startswith("-") and a not in {"auto", "short"}]
            assert not positional, f"{step_name} names paths {positional}; that overrides testpaths"


def test_every_test_file_is_collected_or_excluded_with_a_reason():
    testpaths = _pytest_ini()["testpaths"] + _own_root_testpaths()
    orphans = [
        path
        for path in _all_test_files()
        if path not in NOT_TEST_MODULES
        and not any(_under(path, tp) for tp in testpaths)
        and not any(_under(path, prefix) for prefix in EXCLUDED)
    ]
    assert not orphans, (
        f"{len(orphans)} test file(s) sit outside CI's collection paths {testpaths}, so they "
        "never run. Add their directory to [tool.pytest.ini_options] testpaths in "
        "pyproject.toml, or list them in EXCLUDED here with a reason and a tracking issue:\n  "
        + "\n  ".join(orphans)
    )


def test_every_exclusion_names_a_reason_an_issue_and_live_files():
    files = _all_test_files()
    testpaths = _pytest_ini()["testpaths"]
    for prefix, (reason, issue) in EXCLUDED.items():
        assert reason.strip(), f"exclusion {prefix} gives no reason"
        assert _ISSUE_URL.match(issue), f"exclusion {prefix} names no tracking issue: {issue!r}"
        assert any(_under(f, prefix) for f in files), (
            f"exclusion {prefix} excuses no test file any more; remove it"
        )
        collected = [tp for tp in testpaths if _under(tp, prefix) or _under(prefix, tp)]
        assert not collected, f"{prefix} is both excluded and collected (via {collected})"


def test_not_test_modules_define_no_tests():
    for path, why in NOT_TEST_MODULES.items():
        source = (ROOT / path).read_text()
        assert why.strip()
        assert not re.search(r"^def test_", source, re.MULTILINE), (
            f"{path} is listed as not a test module but defines tests; collect it instead"
        )
