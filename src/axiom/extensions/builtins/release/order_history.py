# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A compact rolling history of test runs (#2441 item 5).

CI is ephemeral and artifact retention is days. Learning which tests fail when
an area changes needs weeks, so each run appends what it saw to one small file
that rides the same cache pattern as the blast-radius map.

The file is JSON Lines. A runner for any language can write it: a test is an
opaque string, and nothing here knows what it names.

    {"k":"ids","v":1,"ids":["tests/test_a.py::test_x", ...]}
    {"k":"run","run":"..","sha":"..","branch":"..","event":"..","at":"..",
     "prefixes":["src/axiom/memory"],"complete":true,
     "i":[deltas of dictionary indexes, in execution order],
     "o":"pfps..","ms":[milliseconds, ...]}

The first line is the dictionary: each name is stored once however many runs
hold it, which is what keeps 35,000 tests across dozens of runs to a few
megabytes under gzip. Outcomes are ``p`` passed, ``f`` failed, ``s`` skipped.
A run is *complete* when nothing stopped it early.

Reading never raises. A missing, truncated or foreign file is no history, and a
bad line costs only that line: a broken history must never break a test run.
"""

from __future__ import annotations

import gzip
import json
import os
import xml.etree.ElementTree as ET
import zlib
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO

FORMAT_VERSION = 1
DEFAULT_KEEP_RUNS = 60
PREFIX_DEPTH = 5
_OUTCOMES = frozenset("pfs")


@dataclass
class RunRecord:
    """One run: who ran it, which area changed, and every test's outcome."""

    run: str
    sha: str
    branch: str
    event: str
    at: str
    prefixes: tuple[str, ...]
    #: ``(test id, outcome, seconds)`` in execution order.
    tests: list[tuple[str, str, float]] = field(default_factory=list)
    complete: bool = True


def prefixes_of(paths: Iterable[str], depth: int = PREFIX_DEPTH) -> tuple[str, ...]:
    """The directories a change touched, cut to ``depth`` segments, sorted and unique.

    A path may carry git's status letter and a tab (``--name-status``). A file
    at the repository root is the area ``(root)``.
    """
    found: set[str] = set()
    for raw in paths:
        status, tab, rest = raw.partition("\t")
        path = rest if tab and len(status) == 1 and status.isalpha() else raw
        path = path.strip()
        if not path:
            continue
        parent = PurePosixPath(path).parent.parts
        found.add("/".join(parent[:depth]) if parent else "(root)")
    return tuple(sorted(found))


def group_rows(
    rows: Iterable[tuple[str, str, float]],
) -> list[tuple[str, str, float]]:
    """Rows grouped by module, modules in the order each first finished, tests in their own order.

    Workers interleave modules, which makes the sequence of dictionary indexes
    different every run and costs most of the file. Grouped, a module's tests
    are consecutive indexes in every run. It is also how xdist runs a chunk.
    """
    rank: dict[str, int] = {}
    listed = list(rows)
    for name, _outcome, _seconds in listed:
        rank.setdefault(name.partition("::")[0], len(rank))
    return sorted(listed, key=lambda row: rank[row[0].partition("::")[0]])


def _open_text(path: Path, mode: str, compressed: bool | None = None) -> IO[str]:
    if compressed if compressed is not None else path.suffix == ".gz":
        return gzip.open(path, mode + "t", encoding="utf-8")  # type: ignore[return-value]
    return open(path, mode, encoding="utf-8")


def _decode_run(raw: dict, ids: list[str]) -> RunRecord | None:
    try:
        deltas, outcomes, millis = raw["i"], raw["o"], raw["ms"]
        if not len(deltas) == len(outcomes) == len(millis):
            return None
        tests: list[tuple[str, str, float]] = []
        index = 0
        for delta, outcome, ms in zip(deltas, outcomes, millis, strict=True):
            index += int(delta)
            if outcome not in _OUTCOMES:
                return None
            tests.append((ids[index], outcome, int(ms) / 1000))
        return RunRecord(
            run=str(raw["run"]),
            sha=str(raw["sha"]),
            branch=str(raw["branch"]),
            event=str(raw["event"]),
            at=str(raw["at"]),
            prefixes=tuple(str(p) for p in raw["prefixes"]),
            tests=tests,
            complete=bool(raw["complete"]),
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def read_history(path: Path | str) -> list[RunRecord]:
    """Every readable run, oldest first. No file, or a file in no known format, is ``[]``."""
    path = Path(path)
    runs: list[RunRecord] = []
    ids: list[str] | None = None
    try:
        with _open_text(path, "r") as handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(raw, dict):
                    continue
                if raw.get("k") == "ids":
                    if raw.get("v") != FORMAT_VERSION or not isinstance(raw.get("ids"), list):
                        return []
                    ids = [str(item) for item in raw["ids"]]
                elif raw.get("k") == "run" and ids is not None:
                    record = _decode_run(raw, ids)
                    if record is not None:
                        runs.append(record)
    except (OSError, EOFError, UnicodeDecodeError, zlib.error):
        # gzip.BadGzipFile is an OSError. A file cut off mid-write keeps the runs read so far.
        return runs
    return runs


def _dump(path: Path, runs: list[RunRecord]) -> None:
    index: dict[str, int] = {}
    for record in runs:
        for name, _outcome, _seconds in record.tests:
            index.setdefault(name, len(index))
    lines = [
        json.dumps(
            {"k": "ids", "v": FORMAT_VERSION, "ids": list(index)},
            separators=(",", ":"),
            ensure_ascii=False,
        )
    ]
    for record in runs:
        deltas: list[int] = []
        previous = 0
        for name, _outcome, _seconds in record.tests:
            deltas.append(index[name] - previous)
            previous = index[name]
        lines.append(
            json.dumps(
                {
                    "k": "run",
                    "run": record.run,
                    "sha": record.sha,
                    "branch": record.branch,
                    "event": record.event,
                    "at": record.at,
                    "prefixes": list(record.prefixes),
                    "complete": record.complete,
                    "i": deltas,
                    "o": "".join(outcome for _n, outcome, _s in record.tests),
                    "ms": [round(seconds * 1000) for _n, _o, seconds in record.tests],
                },
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with _open_text(temporary, "w", compressed=path.suffix == ".gz") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(temporary, path)


def write_history(
    path: Path | str, runs: Iterable[RunRecord], keep_runs: int = DEFAULT_KEEP_RUNS
) -> None:
    """Write ``runs`` (oldest first) as the whole file, keeping the newest ``keep_runs``."""
    _dump(Path(path), list(runs)[-max(1, keep_runs) :])


def append_run(path: Path | str, record: RunRecord, keep_runs: int = DEFAULT_KEEP_RUNS) -> None:
    """Add one run, keep the newest ``keep_runs``, and rewrite the file whole.

    The rewrite rebuilds the dictionary from the runs that remain, so a test
    deleted from the suite leaves the file once the runs that held it do.
    """
    path = Path(path)
    runs = [*read_history(path), record][-max(1, keep_runs) :]
    _dump(path, runs)


def flaky_ids(runs: Iterable[RunRecord]) -> set[str]:
    """Tests that failed and passed on the very same commit.

    Nothing about the code changed between those runs, so the test is the
    unstable part. An incomplete run is not evidence: it may have stopped
    before the test got its second chance.
    """
    outcomes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in runs:
        if not record.complete:
            continue
        for name, outcome, _seconds in record.tests:
            if outcome in "pf":
                outcomes[(record.sha, name)].add(outcome)
    return {name for (_sha, name), seen in outcomes.items() if seen == {"p", "f"}}


def nodeid_from_junit(classname: str, name: str, exists: Callable[[str], bool]) -> str:
    """A pytest node id from JUnit's dotted ``classname`` and ``name``.

    JUnit joins the module path with dots, so ``tests.infra.test_a`` is
    ``tests/infra/test_a.py``. ``exists`` says which files are real, which is
    the only way to tell a package from a class. A name that matches no file
    keeps its dotted form instead of a guess.
    """
    parts = classname.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = "/".join(parts[:cut]) + ".py"
        if exists(candidate):
            return "::".join([candidate, *parts[cut:], name])
    return f"{classname}::{name}"


def run_from_junit(
    xml_text: str,
    *,
    run: str,
    sha: str,
    branch: str,
    event: str,
    at: str,
    changed_paths: Iterable[str],
    exists: Callable[[str], bool],
) -> RunRecord:
    """A run record from a pytest ``--junitxml`` file: the history that already exists."""
    tests: list[tuple[str, str, float]] = []
    for case in ET.fromstring(xml_text).iter("testcase"):
        classname = case.get("classname") or ""
        if not classname:
            continue  # a collection-level skip names a module, not a test
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "f"
        elif case.find("skipped") is not None:
            outcome = "s"
        else:
            outcome = "p"
        tests.append(
            (
                nodeid_from_junit(classname, case.get("name") or "", exists),
                outcome,
                float(case.get("time") or 0),
            )
        )
    return RunRecord(
        run, sha, branch, event, at, prefixes_of(changed_paths), group_rows(tests), True
    )
