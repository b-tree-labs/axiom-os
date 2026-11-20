# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The acceptance harness: every spec row, the tests that carry its ID, and a verdict.

The row registry **is** the spec: every Markdown table row whose first cell is
an acceptance ID (``MVP-02-A1``, ``VAR-A3``, ``P0-ACC-A1``) is a row. Rows
above the spec's ``## P1`` heading are phase P0; rows from it on are P1.

A row is proven only by a test that carries its ID:

- Python: ``@pytest.mark.acceptance("MVP-02-A1")`` on the test, its class, or
  the module (``pytestmark``); or a test named ``test_mvp_02_a1_…``.
- TypeScript / JavaScript: the test title starts with the ID,
  ``it("MVP-02-A1 …")`` (``test(…)`` and ``describe(…)`` likewise).

and the verdict follows the results, never the presence of code:

- ``pass``: every tagged test ran and passed;
- ``fail``: any tagged test failed;
- ``unproven``: no test carries the ID, or a tagged test has not run (or was
  skipped). A tag is a claim; only a result is proof.

Results come from the last ``--run`` (cached under the state directory) or from
a results file a repository's own CI writes (JUnit XML for pytest, the vitest
JSON reporter). The repositories are named in a small TOML file, so one report
spans the platform, the UI kit, a product and a site.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: An acceptance ID as the spec writes it.
ID_RE = re.compile(r"^(?:MVP-\d{2}-A\d+|VAR-A\d+|P\d-[A-Z]+-A\d+)$")
#: The ID at the start of a JS test title.
_JS_TEST = re.compile(
    r"""\b(?:it|test|describe)(?:\.(?:only|skip|each\([^)]*\)))?\(\s*(["'`])"""
    r"""((?:MVP-\d{2}-A\d+|VAR-A\d+|P\d-[A-Z]+-A\d+)\b[^"'`]*)\1"""
)
#: A Python test named for its row: ``test_mvp_02_a1_…``, ``test_var_a3_…``, ``test_p0_acc_a1_…``.
_PY_NAME = re.compile(r"^test_(mvp_\d{2}_a\d+|var_a\d+|p\d_[a-z]+_a\d+)(?:_|$)")

PHASES = ("P0", "P1", "all")
STATUSES = ("pass", "fail", "unproven")

_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "dist", "build", "__pycache__",
              ".pytest_cache", ".mypy_cache", "coverage"}
_JS_SUFFIXES = (".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx")


class SpecError(ValueError):
    """The spec cannot serve as a row registry (for example, a duplicate ID)."""


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    id: str
    phase: str
    item: str
    text: str


def parse_rows(spec_text: str) -> list[Row]:
    rows: list[Row] = []
    seen: dict[str, int] = {}
    phase = "P0"
    item = ""
    for lineno, line in enumerate(spec_text.splitlines(), 1):
        if line.startswith("## "):
            if re.match(r"^## P1\b", line):
                phase = "P1"
            item = ""
            continue
        if line.startswith("### "):
            item = line[4:].strip()
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or not ID_RE.match(cells[0]):
            continue
        rid = cells[0]
        if rid in seen:
            raise SpecError(f"duplicate acceptance ID {rid} (lines {seen[rid]} and {lineno}); "
                            "every row needs its own ID")
        seen[rid] = lineno
        rows.append(Row(id=rid, phase=phase, item=item, text=cells[1] if len(cells) > 1 else ""))
    return rows


# --------------------------------------------------------------------------
# Tests that carry an ID
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TestRef:
    __test__ = False  # not a pytest class

    repo: str
    file: str
    name: str
    nodeid: str


def _iter_files(root: Path, include: list[str], suffixes: tuple[str, ...], prefix: str = ""):
    seen: set[Path] = set()
    for inc in include or ["."]:
        base = (root / inc).resolve()
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for fn in sorted(filenames):
                if fn.endswith(suffixes) and fn.startswith(prefix):
                    p = Path(dirpath) / fn
                    if p not in seen:
                        seen.add(p)
                        yield p


def _marker_ids(decorators: list[ast.expr]) -> list[str]:
    ids: list[str] = []
    for d in decorators:
        if not isinstance(d, ast.Call):
            continue
        f = d.func
        if isinstance(f, ast.Attribute) and f.attr == "acceptance":
            for a in d.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    ids.append(a.value)
    return ids


def _pytestmark_ids(tree: ast.Module) -> list[str]:
    ids: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            vals = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            ids += _marker_ids(list(vals))
    return ids


def _name_id(func: str) -> str | None:
    m = _PY_NAME.match(func)
    return m.group(1).upper().replace("_", "-") if m else None


def scan_python(
    root: Path, include: list[str], *, repo: str = "", errors: list[str] | None = None
) -> dict[str, list[TestRef]]:
    found: dict[str, list[TestRef]] = {}

    def add(rid: str, rel: str, name: str) -> None:
        ref = TestRef(repo=repo, file=rel, name=name, nodeid=f"{rel}::{name}")
        bucket = found.setdefault(rid, [])
        if ref not in bucket:
            bucket.append(ref)

    for path in _iter_files(Path(root), include, (".py",), prefix="test_"):
        rel = path.relative_to(Path(root).resolve()).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            if errors is not None:
                errors.append(f"{repo or root}: could not parse {rel}: {exc}")
            continue
        module_ids = _pytestmark_ids(tree)

        def visit_func(fn: ast.FunctionDef | ast.AsyncFunctionDef, owner: str,
                       inherited: list[str]) -> None:
            if not fn.name.startswith("test"):
                return
            name = f"{owner}::{fn.name}" if owner else fn.name
            ids = set(inherited) | set(_marker_ids(fn.decorator_list))
            named = _name_id(fn.name)
            if named:
                ids.add(named)
            for rid in sorted(ids):
                add(rid, rel, name)

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit_func(node, "", module_ids)
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                cls_ids = module_ids + _marker_ids(node.decorator_list)
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        visit_func(sub, node.name, cls_ids)
    return found


def scan_js(root: Path, include: list[str], *, repo: str = "") -> dict[str, list[TestRef]]:
    found: dict[str, list[TestRef]] = {}
    for path in _iter_files(Path(root), include, _JS_SUFFIXES):
        rel = path.relative_to(Path(root).resolve()).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _JS_TEST.finditer(text):
            title = m.group(2).strip()
            rid = title.split()[0]
            ref = TestRef(repo=repo, file=rel, name=title, nodeid=f"{rel}::{title}")
            bucket = found.setdefault(rid, [])
            if ref not in bucket:
                bucket.append(ref)
    return found


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

_SEVERITY = {"failed": 3, "skipped": 2, "passed": 1}


def _merge(results: dict[str, str], key: str, status: str) -> None:
    """Fold a parameter's result into its test's: any failure fails the test."""
    prior = results.get(key)
    if prior is None or _SEVERITY.get(status, 0) > _SEVERITY.get(prior, 0):
        results[key] = status


def load_junit(path: Path) -> dict[str, str]:
    """``file::[Class::]test`` → passed / failed / skipped, from pytest's JUnit XML."""
    results: dict[str, str] = {}
    raw = path.read_bytes()
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        # pytest's JUnit XML never declares entities; one that does is not ours.
        raise ValueError(f"{path}: DTD/entity declarations are refused")
    root = ET.fromstring(raw)  # nosemgrep (DTD refused above)
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = re.sub(r"\[.*\]$", "", case.get("name", ""))
        if case.find("failure") is not None or case.find("error") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        parts = classname.split(".")
        # The classname is the module's dotted path, then any classes; classes
        # are capitalised by pytest convention (``TestX``), modules are not.
        split = len(parts)
        while split > 0 and parts[split - 1][:1].isupper():
            split -= 1
        module = "/".join(parts[:split]) + ".py"
        owner = "::".join(parts[split:])
        key = f"{module}::{owner}::{name}" if owner else f"{module}::{name}"
        _merge(results, key, status)
    return results


def load_vitest_json(path: Path, *, repo_root: Path) -> dict[str, str]:
    """``file::title`` → status, from vitest's (or jest's) JSON reporter."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    results: dict[str, str] = {}
    root = Path(repo_root).resolve()
    for suite in data.get("testResults") or []:
        name = Path(suite.get("name") or "")
        try:
            rel = name.resolve().relative_to(root).as_posix()
        except ValueError:
            rel = name.as_posix()
        for a in suite.get("assertionResults") or []:
            status = {"passed": "passed", "failed": "failed"}.get(a.get("status"), "skipped")
            _merge(results, f"{rel}::{a.get('title', '')}", status)
            if a.get("fullName"):
                _merge(results, f"{rel}::{a['fullName']}", status)
    return results


def row_status(refs: list[TestRef], results: dict[str, dict[str, str]]) -> tuple[str, str]:
    """(status, reason) for one row from its tagged tests and the known results."""
    if not refs:
        return "unproven", "no test carries this ID"
    got = [results.get(r.repo, {}).get(r.nodeid) for r in refs]
    if any(g == "failed" for g in got):
        n = sum(g == "failed" for g in got)
        return "fail", f"{n} of {len(refs)} tagged test(s) failed"
    if all(g == "passed" for g in got):
        return "pass", f"{len(refs)} tagged test(s) passed"
    missing = sum(g is None for g in got)
    skipped = sum(g == "skipped" for g in got)
    why = []
    if missing:
        why.append(f"{missing} not run")
    if skipped:
        why.append(f"{skipped} skipped")
    return "unproven", f"tagged, but {' and '.join(why)} of {len(refs)}"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class RepoConfig:
    name: str
    path: Path
    lang: str = "python"
    include: list[str] = field(default_factory=lambda: ["."])
    #: A results file the repo's own CI or test run writes (relative to the repo).
    results: str = ""
    #: The interpreter for ``--run`` (Python repos). Default: this one.
    python: str = ""
    #: The command ``--run`` uses for a JS repo; it must write ``results``.
    run: list[str] = field(default_factory=list)


@dataclass
class HarnessConfig:
    spec: Path
    repos: list[RepoConfig]
    source: str = ""


def load_config(path: str | Path, *, base: Path | None = None) -> HarnessConfig:
    """Read ``acceptance.toml``. Paths are relative to the file (or to ``base``)."""
    p = Path(path)
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    root = (base or p.parent).resolve()
    section = data.get("acceptance", {}) or {}
    if not section.get("spec"):
        raise ValueError(f"{p}: [acceptance] spec = \"<path to the spec>\" is required")
    repos = []
    for r in data.get("repo", []) or []:
        if not r.get("name") or not r.get("path"):
            raise ValueError(f"{p}: every [[repo]] needs a name and a path: {r!r}")
        lang = str(r.get("lang", "python"))
        if lang not in {"python", "js"}:
            raise ValueError(f"{p}: repo {r['name']!r}: lang must be python or js")
        run = r.get("run") or []
        repos.append(RepoConfig(
            name=str(r["name"]),
            path=(root / str(r["path"])).resolve(),
            lang=lang,
            include=[str(i) for i in (r.get("include") or ["."])],
            results=str(r.get("results", "") or ""),
            python=str(r.get("python", "") or ""),
            run=[str(a) for a in (run if isinstance(run, list) else str(run).split())],
        ))
    return HarnessConfig(spec=(root / str(section["spec"])).resolve(), repos=repos, source=str(p))


#: The environment variable naming a workspace's ``acceptance.toml``.
CONFIG_ENV = "AXIOM_ACCEPTANCE_CONFIG"
_DEFAULT = Path(__file__).with_name("default_acceptance.toml")


def resolve_config(path: str | None = None, *, cwd: Path | None = None) -> HarnessConfig:
    """``path``, else ``$AXIOM_ACCEPTANCE_CONFIG``, else the nearest ``acceptance.toml``
    from the working directory up, else the platform's own (this repository only)."""
    if path:
        if not Path(path).is_file():
            raise FileNotFoundError(f"no acceptance config at {path}")
        return load_config(path)
    env = os.environ.get(CONFIG_ENV, "").strip()
    if env:
        return resolve_config(env)
    here = (cwd or Path.cwd()).resolve()
    for d in (here, *here.parents):
        candidate = d / "acceptance.toml"
        if candidate.is_file():
            return load_config(candidate)
    from axiom import REPO_ROOT

    return load_config(_DEFAULT, base=Path(REPO_ROOT))


# --------------------------------------------------------------------------
# Scan, run, report
# --------------------------------------------------------------------------


def scan_repo(repo: RepoConfig) -> tuple[dict[str, list[TestRef]], list[str]]:
    errors: list[str] = []
    if not repo.path.is_dir():
        return {}, [f"{repo.name}: not found at {repo.path}"]
    if repo.lang == "js":
        return scan_js(repo.path, repo.include, repo=repo.name), errors
    return scan_python(repo.path, repo.include, repo=repo.name, errors=errors), errors


def file_results(repo: RepoConfig) -> dict[str, str]:
    """Results from the file the repo's own run writes, when configured and present."""
    if not repo.results:
        return {}
    p = repo.path / repo.results
    if not p.is_file():
        return {}
    try:
        if p.suffix == ".xml":
            return load_junit(p)
        return load_vitest_json(p, repo_root=repo.path)
    except (OSError, ValueError, ET.ParseError):
        return {}


def run_repo(repo: RepoConfig, found: dict[str, list[TestRef]], *,
             timeout_s: int = 1800) -> dict[str, str]:
    """Run a repo's tagged tests and return their results. Python: pytest on the
    tagged node IDs only, so the run is as small as the tags. JS: the configured
    command, which must write the configured results file."""
    if repo.lang == "js":
        if not repo.run:
            return {}
        subprocess.run(repo.run, cwd=repo.path, timeout=timeout_s, check=False,
                       stdin=subprocess.DEVNULL, capture_output=True)
        return file_results(repo)
    nodeids = sorted({r.nodeid for refs in found.values() for r in refs})
    if not nodeids:
        return {}
    python = repo.python or sys.executable
    if repo.python and not Path(python).is_absolute():
        python = str(repo.path / python)
    with tempfile.TemporaryDirectory() as td:
        junit = Path(td) / "acceptance.xml"
        subprocess.run(
            [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}",
             "-o", "junit_family=xunit2", *nodeids],
            cwd=repo.path, timeout=timeout_s, check=False, stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        return load_junit(junit) if junit.is_file() else {}


def build_report(cfg: HarnessConfig, *, phase: str = "all",
                 cached: dict[str, dict[str, str]] | None = None,
                 cached_at: str = "") -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}; choose from {list(PHASES)}")
    if not cfg.spec.is_file():
        raise FileNotFoundError(f"the spec is not at {cfg.spec}")
    rows = parse_rows(cfg.spec.read_text(encoding="utf-8"))
    known = {r.id for r in rows}
    found: dict[str, list[TestRef]] = {}
    results: dict[str, dict[str, str]] = {}
    repos_out, errors = [], []
    for repo in cfg.repos:
        refs, errs = scan_repo(repo)
        errors += errs
        for rid, rs in refs.items():
            found.setdefault(rid, []).extend(rs)
        res = dict(file_results(repo))
        res.update((cached or {}).get(repo.name, {}))
        results[repo.name] = res
        repos_out.append({"name": repo.name, "path": str(repo.path), "lang": repo.lang,
                          "found": repo.path.is_dir(),
                          "tagged_tests": sum(len(v) for v in refs.values())})
    out_rows = []
    totals = {s: 0 for s in STATUSES}
    for row in rows:
        if phase != "all" and row.phase != phase:
            continue
        refs = found.get(row.id, [])
        status, reason = row_status(refs, results)
        totals[status] += 1
        out_rows.append({
            "id": row.id, "phase": row.phase, "item": row.item, "acceptance": row.text,
            "status": status, "reason": reason,
            "tests": [{"repo": r.repo, "file": r.file, "name": r.name,
                       "result": results.get(r.repo, {}).get(r.nodeid) or "not run"}
                      for r in refs],
        })
    orphans = [{"id": rid, "tests": [{"repo": r.repo, "file": r.file, "name": r.name}
                                     for r in rs]}
               for rid, rs in sorted(found.items()) if rid not in known]
    return {
        "phase": phase,
        "spec": str(cfg.spec),
        "config": cfg.source,
        "results_from": cached_at,
        "totals": {**totals, "rows": len(out_rows)},
        "proven": totals["pass"] == len(out_rows) and bool(out_rows),
        "rows": out_rows,
        "orphans": orphans,
        "repos": repos_out,
        "errors": errors,
    }


def render(report: dict[str, Any]) -> list[str]:
    from axiom.infra.cli_format import Column, table

    t = report["totals"]
    lines = [f"{report['phase']}: {t['pass']} pass, {t['fail']} fail, {t['unproven']} unproven "
             f"of {t['rows']} rows" + (" (gate met)" if report["proven"] else "")]
    glyph = {"pass": "✓", "fail": "✕", "unproven": "—"}
    rows = []
    for r in report["rows"]:
        tests = ", ".join(f"{x['repo']}:{x['file']}::{x['name']} [{x['result']}]"
                          for x in r["tests"]) or r["reason"]
        rows.append([f"{glyph[r['status']]} {r['status']}", r["id"], tests])
    lines += table(rows, [Column(), Column(), Column(wrap=True)])
    for o in report["orphans"]:
        lines.append(f"  ? tagged with an ID the spec does not have: {o['id']} "
                     f"({', '.join(x['repo'] + ':' + x['file'] for x in o['tests'])})")
    for r in report["repos"]:
        if not r["found"]:
            lines.append(f"  note: repo {r['name']} not found at {r['path']}")
    if report.get("results_from"):
        lines.append(f"  results from the run at {report['results_from']}")
    return lines


__all__ = [
    "CONFIG_ENV",
    "PHASES",
    "STATUSES",
    "HarnessConfig",
    "RepoConfig",
    "Row",
    "SpecError",
    "TestRef",
    "build_report",
    "file_results",
    "load_config",
    "load_junit",
    "load_vitest_json",
    "parse_rows",
    "render",
    "resolve_config",
    "row_status",
    "run_repo",
    "scan_js",
    "scan_python",
    "scan_repo",
]
