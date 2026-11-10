# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A fire-log row that never finishes cannot answer "did it finish?".

`schedule_fire_log` carries `finished_at`, and `record_outcome` accepts it. The
engine never passed it, so every row in the table had `started_at` set and
`finished_at` NULL — permanently.

Found on the first real firing of the nightly backup. The dump was on disk and
the journal said it completed in 13 seconds, but the table said:

    intended_fire_at=2026-09-19 02:00:00+00:00
    started_at      =2026-09-19 02:00:00+00:00
    finished_at     =None

NULL there is indistinguishable from still-running and from crashed — which is
exactly the question an operator asks the morning after.
"""

from __future__ import annotations

import ast
import inspect

from axiom.extensions.builtins.schedule import engine


def _record_outcome_calls() -> list[ast.Call]:
    tree = ast.parse(inspect.getsource(engine))
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "record_outcome"
    ]


def test_there_are_outcome_calls_to_check():
    """A guard over an empty set passes forever."""
    assert len(_record_outcome_calls()) >= 4


def test_every_recorded_outcome_carries_a_finish_time():
    """Success, failure, authz denial, compliance skip, dead letter — each of
    them ENDS the run, so each must say when."""
    missing = [
        c.lineno
        for c in _record_outcome_calls()
        if not any(k.arg == "finished_at" for k in c.keywords)
    ]
    assert not missing, (
        f"record_outcome without finished_at at line(s) {missing} — a row that "
        "records a start and never a finish cannot answer whether the job "
        "completed, which is the question asked the morning after"
    )


def test_the_finish_time_is_read_fresh_not_reused_from_the_tick():
    """`now` is when the TICK began, before the executor ran. Reusing it would
    make every run appear to take zero seconds."""
    src = inspect.getsource(engine)
    for call in _record_outcome_calls():
        kw = next((k for k in call.keywords if k.arg == "finished_at"), None)
        assert kw is not None, f"line {call.lineno}"
        rendered = ast.unparse(kw.value)
        assert rendered != "now", (
            f"line {call.lineno}: finished_at=now is the tick's start time, not "
            "the run's end — the duration would always be zero"
        )
        assert "now_fn" in rendered, f"line {call.lineno}: {rendered}"
    assert "finished_at=ctx.now_fn()" in src


def test_the_store_actually_persists_it():
    """The column and the setter must both exist, or the engine is passing an
    argument into a function that drops it."""
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleFireLog

    assert "finished_at" in ScheduleFireLog.__table__.columns
    # The whole chain, because each link can drop it independently and each
    # break looks fixed from the call site.
    sig = inspect.signature(store.SqlFireLog.record_outcome)
    assert "finished_at" in sig.parameters, (
        "the engine passes finished_at=; this is the method it reaches, and "
        "before this fix it raised TypeError on every single fire"
    )

    # record_outcome must FORWARD it, not accept and ignore it.
    forwards = inspect.getsource(store.SqlFireLog.record_outcome)
    assert "finished_at=finished_at" in forwards, (
        "record_outcome takes finished_at and does not pass it on — accepting "
        "an argument you drop is worse than refusing it, because the caller "
        "looks correct"
    )

    # and _update must actually write it to the row.
    assert "finished_at" in inspect.signature(store.SqlFireLog._update).parameters
    assert "row.finished_at = finished_at" in inspect.getsource(store.SqlFireLog._update)
