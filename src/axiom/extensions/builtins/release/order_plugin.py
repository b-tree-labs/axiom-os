# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one pytest plugin behind failed-first, ranked order and the run history (#2441).

It is loaded with ``-p axiom.extensions.builtins.release.order_plugin``, which CI
passes through ``PYTEST_ADDOPTS`` so the unit job stays a bare ``pytest``. With no
setting it does nothing at all. Settings come from the environment:

``AXIOM_TEST_ORDER``
    ``off`` (the default), ``failed-first`` (the last run's failures first) or
    ``ranked`` (failed-first, then chance of failing per second of runtime). The
    ranker is shadow-only until ``order_replay`` shows it gains: nothing turns it
    on except this one explicit value.
``AXIOM_TEST_HISTORY``
    The history file to read and append to (``.jsonl``, or ``.jsonl.gz``).
    Recording happens only when this is set.
``AXIOM_TEST_CHANGED_FILES``
    A file of changed paths, one per line (``git diff --name-only`` or
    ``--name-status``), whose directories name the area this run changes.
``AXIOM_TEST_SHARD``
    ``<i>/<n>`` (one-based): run only this shard's modules (``order_shard``). A malformed
    value stops the run. ``AXIOM_TEST_SHARD_TABLE`` names the duration table (default
    ``scripts/unit_shard_durations.json``) and ``AXIOM_TEST_SHARD_REPORT`` a file that gets
    the shard's report for ``order_shard verify``.
``AXIOM_TEST_HISTORY_OUT``
    Where to write this run's record instead of appending to ``AXIOM_TEST_HISTORY``.
    A shard writes a fragment; the job that joins the shards merges the fragments.
``AXIOM_TEST_HISTORY_KEEP``
    How many runs the file keeps.

The plugin must never change a verdict. Every read and write of the history is
guarded: a corrupt file is no history, an unwritable one is a warning. Under
xdist every worker collects and orders for itself, so the order is a pure
function of the history file and the collected ids: the workers agree. The
controller, which sees every worker's reports, writes the one record.
"""

from __future__ import annotations

import json
import os
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.order_history import (
    DEFAULT_KEEP_RUNS,
    RunRecord,
    append_run,
    group_rows,
    prefixes_of,
    read_history,
    write_history,
)
from axiom.extensions.builtins.release.order_rank import MODES, order_ids
from axiom.extensions.builtins.release.order_shard import (
    ShardReport,
    digest,
    load_table,
    parse_spec,
    select,
)


def _mode(environ: dict[str, str]) -> str:
    mode = environ.get("AXIOM_TEST_ORDER", "off").strip() or "off"
    if mode not in MODES:
        warnings.warn(
            f"AXIOM_TEST_ORDER={mode!r} is not one of {MODES}; the order is left alone",
            stacklevel=2,
        )
        return "off"
    return mode


def _changed(environ: dict[str, str]) -> list[str]:
    path = environ.get("AXIOM_TEST_CHANGED_FILES", "")
    if not path:
        return []
    try:
        return [line for line in Path(path).read_text().splitlines() if line.strip()]
    except OSError:
        return []


def _workers(config: pytest.Config) -> int:
    """Parallel workers under worksteal, which cuts the list into contiguous blocks."""
    try:
        count = int(getattr(config.option, "numprocesses", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return count if getattr(config.option, "dist", "") == "worksteal" else 0


class _Reorder:
    def __init__(self, mode: str, environ: dict[str, str]) -> None:
        self.mode = mode
        self.environ = environ

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        try:
            history = self.environ.get("AXIOM_TEST_HISTORY", "")
            runs = read_history(history) if history else []
            ids = [item.nodeid for item in items]
            ordered = order_ids(
                ids,
                runs,
                prefixes_of(_changed(self.environ)),
                self.mode,
                workers=_workers(config),
            )
        except Exception as error:  # an ordering bug must cost nothing but the ordering
            warnings.warn(f"test order skipped: {error}", stacklevel=1)
            return
        position = {tid: index for index, tid in enumerate(ordered)}
        items.sort(key=lambda item: position[item.nodeid])


class _Shard:
    """Keep only this shard's modules. Runs last, after ``-m`` has deselected what it will."""

    def __init__(self, index: int, count: int, table_path: str, report_path: str) -> None:
        self.index, self.count = index, count
        self.table_path, self.report_path = table_path, report_path

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        ids = [item.nodeid for item in items]
        keep = set(select(ids, self.index, self.count, load_table(self._table(config))))
        dropped = [item for item in items if item.nodeid not in keep]
        if self.report_path:
            report = ShardReport(
                index=self.index + 1,
                count=self.count,
                total=len(ids),
                total_digest=digest(ids),
                selected=len(keep),
                selected_digest=digest(keep),
            )
            _write_json(self.report_path, report.__dict__)
        if dropped:
            config.hook.pytest_deselected(items=dropped)
            items[:] = [item for item in items if item.nodeid in keep]

    def _table(self, config: pytest.Config) -> Path:
        path = Path(self.table_path)
        return path if path.is_absolute() else Path(config.rootpath) / path


def _write_json(path: str, body: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(body), encoding="utf-8")
    os.replace(temporary, target)


class _Record:
    """Runs in the process that sees every report: the controller under xdist."""

    def __init__(self, environ: dict[str, str]) -> None:
        self.environ = environ
        self._seen: dict[str, list] = {}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        entry = self._seen.setdefault(report.nodeid, ["p", 0.0])
        entry[1] += report.duration
        if report.failed:
            entry[0] = "f"
        elif report.skipped and entry[0] != "f":
            entry[0] = "s"

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if session.config.option.collectonly or not self._seen:
            return
        env = self.environ
        maxfail = int(getattr(session.config.option, "maxfail", 0) or 0)
        stopped = bool(session.shouldstop) or int(exitstatus) == 2
        stopped = stopped or (maxfail > 0 and session.testsfailed >= maxfail)
        record = RunRecord(
            run=env.get("GITHUB_RUN_ID") or f"local-{int(time.time())}",
            sha=env.get("GITHUB_SHA", ""),
            branch=env.get("GITHUB_HEAD_REF") or env.get("GITHUB_REF_NAME", ""),
            event=env.get("GITHUB_EVENT_NAME", "local"),
            at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            prefixes=prefixes_of(_changed(env)),
            tests=group_rows(
                (tid, outcome, seconds) for tid, (outcome, seconds) in self._seen.items()
            ),
            complete=not stopped,
        )
        try:
            keep = int(env.get("AXIOM_TEST_HISTORY_KEEP", DEFAULT_KEEP_RUNS))
            out = env.get("AXIOM_TEST_HISTORY_OUT")
            if out:
                write_history(out, [record], keep_runs=1)
            else:
                append_run(env["AXIOM_TEST_HISTORY"], record, keep_runs=keep)
        except Exception as error:  # never let bookkeeping change the verdict
            warnings.warn(f"test history not written: {error}", stacklevel=1)


#: Set in the environment by the outermost session. Whatever launches a pytest under it, in a
#: subprocess or in process, finds it, and that pytest does nothing (see ``pytest_configure``).
_SESSION_MARK = "AXIOM_PYTEST_OUTER_SESSION"


def pytest_configure(config: pytest.Config) -> None:
    # The settings below are the CI run's, not those of a pytest a test of that run launches
    # (the extension standard tests, `axi ext test`, the isolation guard, the acceptance
    # harness). Left active there they deselect that pytest's tests, rewrite the shard's report
    # and append to its history: the first sharded main run failed 27, 5 and 15 tests on it
    # and its shards' totals disagreed. The outermost session marks the environment; its xdist
    # workers belong to it; anything else that finds the mark is nested and inert.
    in_worker = hasattr(config, "workerinput")
    if os.environ.get(_SESSION_MARK) and not in_worker:
        return
    os.environ[_SESSION_MARK] = "1"
    environ = dict(os.environ)
    mode = _mode(environ)
    if mode != "off":
        config.pluginmanager.register(_Reorder(mode, environ), "axiom-test-reorder")
    is_worker = hasattr(config, "workerinput")
    spec = environ.get("AXIOM_TEST_SHARD", "").strip()
    if spec:
        try:
            index, count = parse_spec(spec)
        except ValueError as error:
            raise pytest.UsageError(f"AXIOM_TEST_SHARD: {error}") from error
        recording = any(
            getattr(config.option, name, False)
            for name in ("testmon", "testmon_noselect", "testmon_forceselect")
        )
        in_worker = hasattr(config, "workerinput")
        if recording and not in_worker and not getattr(config.option, "numprocesses", 0):
            # Without xdist testmon also records the tests a shard deselected, with no
            # dependencies: a map that would send a pull request past them. Refuse.
            raise pytest.UsageError(
                "AXIOM_TEST_SHARD with testmon needs xdist (-n 1 or more): without it testmon "
                "records the tests this shard did not run"
            )
        config.pluginmanager.register(
            _Shard(
                index,
                count,
                environ.get("AXIOM_TEST_SHARD_TABLE", "scripts/unit_shard_durations.json"),
                environ.get("AXIOM_TEST_SHARD_REPORT", ""),
            ),
            "axiom-test-shard",
        )
    if (
        environ.get("AXIOM_TEST_HISTORY") or environ.get("AXIOM_TEST_HISTORY_OUT")
    ) and not is_worker:
        config.pluginmanager.register(_Record(environ), "axiom-test-record")
