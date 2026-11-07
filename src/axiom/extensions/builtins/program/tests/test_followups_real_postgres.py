# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Follow-ups against real processes and a real Postgres (ADR-196).

Nothing here is faked. A Postgres runs in a ``fu-<hex>`` container (never the
developer's database; removed by name, nothing pruned). Follow-ups are created
through the real composed MCP server over stdio, as a harness would. The clock
is the real orchestrator process, killed with ``kill -9`` and restarted. The
delivery is HERALD into the real notifications inbox table.

What it proves:

- a follow-up fires exactly once across a ``kill -9`` and a restart, including
  a kill in the worst window (the message sent, the delivery not yet recorded);
- the check's current state is in the delivered message;
- silence climbs the ladder, to the people the ladder names, in order;
- an acknowledgement stops it, a snooze moves it;
- a follow-up created over MCP by one principal is invisible to another's list;
- no secret value is accepted, stored, journaled or delivered;
- a follow-up that came due while nothing was running is reported missed.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import signal
import subprocess
import sys
import time

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("mcp")

from axiom.extensions.builtins.data_platform.tests import timescale_container  # noqa: E402
from axiom.infra.secret_scan import scan_text  # noqa: E402

from .conftest import GENERIC_PROGRAM  # noqa: E402

BEA = "@bea:local"
DOV = "@dov:local"
CASEY = "@casey:local"
OPS = "@ops:local"
SECRET = "glpat-" + "a1B2c3D4e5F6g7H8i9J0"

# Real processes and a container: the platform's "integration" tier (external services, real
# runtime). It runs in the merge queue and on main (the Integration job); locally:
#   pytest -m integration --override-ini=addopts= src/axiom/extensions/builtins/program/tests/test_followups_real_postgres.py
pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


@pytest.fixture(scope="module")
def dsn():
    os.environ[timescale_container.PREFIX_ENV] = "fu"
    try:
        name, url = timescale_container.start()
    finally:
        os.environ.pop(timescale_container.PREFIX_ENV, None)
    assert name.startswith("fu-")
    yield url
    timescale_container.stop(name)


@pytest.fixture
def world(dsn, tmp_path):
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("AXIOM_PROC_MARK", "AXIOM_TEST_SCHEMA_SUFFIX", "GITLAB_TOKEN", "GITHUB_TOKEN")}
    env.update({
        "AXIOM_DB_URL": dsn, "AXI_STATE_DIR": str(state), "AXIOM_STATE_DIR": str(state),
        "AXI_ORCHESTRATOR_TICK_SECONDS": "1", "AXI_CHANGE_LEDGER": str(tmp_path / "changes.jsonl"),
        "AXI_FOLLOWUP_OPERATOR": OPS, "PYTHONUNBUFFERED": "1", "AXI_DIAGNOSES_QUIET": "1",
    })
    with psycopg.connect(dsn, autocommit=True) as c:
        for schema in ("schedule", "program", "notifications"):
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    done = subprocess.run(
        [sys.executable, "-c",
         "from axiom.infra.db import provision_extension as p; "
         "r = [p(e) for e in ('schedule', 'program', 'notifications')]; "
         "print(r); raise SystemExit(0 if all(x.ok for x in r) else 1)"],
        env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    yield env, state, dsn
    with psycopg.connect(dsn, autocommit=True) as c:
        for schema in ("schedule", "program", "notifications"):
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


# ---- helpers ---------------------------------------------------------------


def _sql(dsn, query, *args):
    with psycopg.connect(dsn, autocommit=True) as c:
        cur = c.execute(query, args)
        return cur.fetchall() if cur.description else []


def _inbox(dsn):
    return _sql(dsn, "SELECT recipient, priority, summary FROM notifications.notifications_inbox ORDER BY created_at, id")


def _events(dsn, fid, kind=None):
    q = "SELECT kind, rung, detail FROM program.followup_event WHERE followup_id = %s"
    if kind:
        return _sql(dsn, q + " AND kind = %s ORDER BY seq", fid, kind)
    return _sql(dsn, q + " ORDER BY seq", fid)


def _wait(cond, what, timeout=90.0, show=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = cond()
        if got:
            return got
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}" + (f"; saw {show()}" if show else ""))


async def _session(env, principal, calls):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    e = dict(env)
    e["AXIOM_MCP_CLIENT_PRINCIPAL"] = principal
    params = StdioServerParameters(command=sys.executable, args=["-m", "axiom.extensions.builtins.mcp.server"], env=e)
    out = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            for tool, args in calls:
                res = await s.call_tool(tool, args)
                out.append(json.loads(res.content[0].text))
            out.append({"_tools": tools})
    return out


def mcp(env, principal, *calls):
    """Call tools over the real composed MCP server, as ``principal``. Returns each result."""
    got = asyncio.run(_session(env, principal, [
        ("axiom_program__status" if t == "status" else f"axiom_program__followup_{t}", a) for t, a in calls]))
    return got[:-1] if len(calls) else got


def mcp_tools(env, principal=BEA):
    return asyncio.run(_session(env, principal, []))[-1]["_tools"]


def _start(env, tmp_path, tag):
    log = open(tmp_path / f"orchestrator-{tag}.log", "wb")
    return subprocess.Popen(
        [sys.executable, "-m", "axiom.extensions.builtins.data_platform.orchestration"],
        env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def _reap(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if p.poll() is None:
        p.wait()


def _up(dsn, p):
    """The orchestrator has registered the follow-up heartbeat: it is ticking."""
    return lambda: p.poll() is None and _sql(
        dsn, "SELECT 1 FROM schedule.schedule_definition WHERE action = 'program.followup_sweep'")


@pytest.fixture
def orchestrators(world, tmp_path):
    env, _, _ = world
    started: list = []

    def start(tag):
        p = _start(env, tmp_path, tag)
        started.append(p)
        return p

    yield start
    for p in started:
        _reap(p)


def _no_secrets_anywhere(dsn):
    dump = json.dumps({
        "followup": [list(map(str, r)) for r in _sql(dsn, "SELECT * FROM program.followup")],
        "events": [list(map(str, r)) for r in _sql(dsn, "SELECT * FROM program.followup_event")],
        "inbox": [list(map(str, r)) for r in _sql(dsn, "SELECT * FROM notifications.notifications_inbox")],
    })
    assert not scan_text(dump), "a credential-format value reached the follow-up tables or the inbox"
    assert SECRET not in dump


# ---- the tests --------------------------------------------------------------


def test_the_followup_tools_are_on_the_composed_mcp_with_the_right_hints(world):
    env, _, _ = world
    tools = mcp_tools(env)
    for verb, read_only in (("add", False), ("list", True), ("show", True), ("owed", True),
                            ("snooze", False), ("ack", False), ("done", False)):
        t = tools[f"axiom_program__followup_{verb}"]
        assert t.annotations.read_only_hint is read_only, verb
        assert not t.annotations.destructive_hint, verb
    assert not any(n.endswith(("followup_fire", "followup_sweep")) for n in tools)


def test_a_due_followup_fires_once_across_a_kill_9_and_a_restart(world, orchestrators, tmp_path):
    env, _, dsn = world
    added = mcp(env, BEA, ("add", {"what": "check on the drop-old task", "due": "40s"}))[0]
    assert added["ok"], added
    fid = added["value"]["id"]

    first = orchestrators("first")
    _wait(_up(dsn, first), "the first orchestrator to tick")
    os.kill(first.pid, signal.SIGKILL)  # killed before the follow-up is due
    first.wait(timeout=30)
    assert _inbox(dsn) == []

    _wait(lambda: _sql(dsn, "SELECT now() > due_at FROM program.followup WHERE id = %s", fid)[0][0], "the due time", 60)
    orchestrators("second")
    _wait(lambda: _inbox(dsn), "the reminder to be delivered", 90,
          show=lambda: (_sql(dsn, "SELECT * FROM schedule.schedule_fire_log"), _events(dsn, fid)))
    time.sleep(8)  # many more ticks: nothing may fire it a second time

    rows = _inbox(dsn)
    assert len(rows) == 1 and rows[0][0] == BEA and f"[{fid}]" in rows[0][2], rows
    assert len(_events(dsn, fid, "delivered")) == 1
    fires = _sql(dsn, "SELECT f.outcome FROM schedule.schedule_fire_log f JOIN schedule.schedule_definition d "
                      "ON d.id = f.schedule_id WHERE d.action = 'program.followup_fire' AND d.capability_envelope::text LIKE %s",
                 f"%{fid}%")
    assert [f[0] for f in fires].count("success") == 1, fires
    _no_secrets_anywhere(dsn)


def test_a_kill_9_after_the_send_and_before_the_delivery_is_recorded_still_delivers_once(world, orchestrators):
    env, _, dsn = world
    fid = mcp(env, BEA, ("add", {"what": "verify the swap", "due": "20s"}))[0]["value"]["id"]
    # Hold the "delivered" journal row's unique key open in an uncommitted transaction. The fire
    # sends the message, then blocks writing "delivered": the worst window for a crash.
    holder = psycopg.connect(dsn, autocommit=False)
    holder.execute(
        "INSERT INTO program.followup_event (followup_id, at, kind, actor, cycle, rung, detail, dedup_key) "
        "VALUES (%s, now(), 'delivered', 'test', 0, 0, '{}', %s)", (fid, f"delivered:{fid}:0:0"))
    first = orchestrators("first")
    try:
        _wait(lambda: _inbox(dsn), "the message to be sent", 90)
        pending = _wait(lambda: _sql(dsn, "SELECT owner_pid FROM schedule.schedule_fire_log WHERE outcome = 'pending'"),
                        "the fire to be in flight", 30)
        assert pending[0][0] == first.pid
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=30)
    finally:
        holder.rollback()  # the crashed process never recorded it
        holder.close()
    assert len(_inbox(dsn)) == 1 and _events(dsn, fid, "delivered") == []

    orchestrators("second")
    _wait(lambda: _events(dsn, fid, "delivered"), "the re-run to record the delivery", 90,
          show=lambda: _sql(dsn, "SELECT outcome, owner_pid FROM schedule.schedule_fire_log"))
    time.sleep(5)
    assert len(_inbox(dsn)) == 1, "HERALD suppressed the duplicate: one message, not two"
    [(_, _, detail)] = _events(dsn, fid, "delivered")
    assert detail["suppressed_duplicate"] is True
    assert _sql(dsn, "SELECT rung, state FROM program.followup WHERE id = %s", fid) == [(1, "due")]


def test_the_checks_current_state_is_in_the_delivered_message(world, orchestrators):
    env, _, dsn = world
    added = mcp(env, BEA, ("add", {"what": "see how many I owe", "due": "6s", "check": "program.followup_owed",
                                   "next_command": "axi program followup done --id <id>"}))[0]
    assert added["ok"], added
    orchestrators("only")
    [(recipient, priority, summary)] = _wait(lambda: _inbox(dsn), "the reminder", 90)
    assert recipient == BEA and priority == "normal"
    assert "check program.followup_owed passed" in summary and "total=1" in summary
    assert "next: axi program followup done --id <id>" in summary
    [(_, _, detail)] = _events(dsn, added["value"]["id"], "check_ran")
    assert detail["ok"] is True and detail["fields"]["total"] == 1
    _no_secrets_anywhere(dsn)


def test_silence_climbs_the_ladder_to_the_people_it_names_in_order(world, orchestrators):
    env, state, dsn = world
    program = json.loads(json.dumps(copy.deepcopy(GENERIC_PROGRAM)).replace("@casey:example-org", CASEY))
    (state / "program" / "data.json").write_text(json.dumps(program))
    ladder = [{"after_s": 0, "audience": "owner", "kind": "reminder"},
              {"after_s": 5, "audience": "owner", "kind": "reminder"},
              {"after_s": 10, "audience": "requester", "kind": "flag"},
              {"after_s": 15, "audience": "operator", "kind": "contact_the_owner"}]
    added = mcp(env, CASEY, ("add", {"what": "approve the drop-old", "due": "6s", "owner": DOV, "ladder": ladder}))[0]
    assert added["ok"], added
    fid = added["value"]["id"]
    assert added["value"]["owner"] == DOV and added["value"]["created_by"] == CASEY
    orchestrators("only")
    rows = _wait(lambda: len(_inbox(dsn)) >= 4 and _inbox(dsn), "four rungs to be delivered", 120,
                 show=lambda: (_inbox(dsn), _events(dsn, fid)))
    assert [(r[0], r[1]) for r in rows] == [(DOV, "normal"), (DOV, "normal"), (CASEY, "high"), (OPS, "high")]
    assert "not acknowledged by" in rows[2][2] and "please contact" in rows[3][2]
    # nobody answered, so the journal says each rung went out, in order
    assert [e[1] for e in _events(dsn, fid, "delivered")] == [0, 1, 2, 3]
    _no_secrets_anywhere(dsn)


def test_an_acknowledgement_stops_the_ladder(world, orchestrators):
    env, _, dsn = world
    # The later rungs are far enough out that a slow runner (a cold MCP server takes seconds to
    # start) still acknowledges before the second one, and the wait runs on the database clock.
    ladder = [{"after_s": 0, "audience": "owner", "kind": "reminder"},
              {"after_s": 45, "audience": "owner", "kind": "reminder"},
              {"after_s": 90, "audience": "owner", "kind": "reminder"}]
    fid = mcp(env, DOV, ("add", {"what": "rotate the overlap key", "due": "5s", "ladder": ladder}))[0]["value"]["id"]
    orchestrators("only")
    _wait(lambda: _inbox(dsn), "the first reminder", 90)
    acked = mcp(env, DOV, ("ack", {"id": fid, "note": "on it"}))[0]
    assert acked["ok"] and acked["value"]["state"] == "acknowledged", acked
    _wait(lambda: _sql(dsn, "SELECT now() > due_at + interval '50 seconds' FROM program.followup WHERE id = %s", fid)[0][0],
          "the second rung's time to pass", 120)
    time.sleep(6)  # and a few ticks past it
    assert len(_inbox(dsn)) == 1
    assert _sql(dsn, "SELECT state FROM program.followup WHERE id = %s", fid) == [("acknowledged",)]
    assert _sql(dsn, "SELECT count(*) FROM schedule.schedule_definition WHERE action = 'program.followup_fire' "
                     "AND state = 'active' AND next_fire_at IS NOT NULL") == [(0,)]
    closed = mcp(env, DOV, ("done", {"id": fid, "note": "dropped"}))[0]
    assert closed["ok"] and closed["value"]["state"] == "done"


def test_a_snooze_moves_the_due_time(world, orchestrators):
    env, _, dsn = world
    fid = mcp(env, BEA, ("add", {"what": "check the rollback window", "due": "1h"}))[0]["value"]["id"]
    before = _sql(dsn, "SELECT due_at, schedule_id FROM program.followup WHERE id = %s", fid)[0]
    snoozed = mcp(env, BEA, ("snooze", {"id": fid, "by": "6s"}))[0]
    assert snoozed["ok"] and snoozed["value"]["state"] == "snoozed", snoozed
    after = _sql(dsn, "SELECT due_at, schedule_id FROM program.followup WHERE id = %s", fid)[0]
    assert after[0] < before[0] and after[1] != before[1], "the due time moved and the clock row was replaced"
    assert _sql(dsn, "SELECT state FROM schedule.schedule_definition WHERE id = %s", before[1]) == [("cancelled",)]
    orchestrators("only")
    [(recipient, _, summary)] = _wait(lambda: _inbox(dsn), "the snoozed reminder", 90)
    assert recipient == BEA and f"[{fid}]" in summary
    assert _sql(dsn, "SELECT state FROM program.followup WHERE id = %s", fid) == [("due",)]


def test_a_followup_made_over_mcp_by_one_principal_is_invisible_to_another(world):
    env, _, dsn = world
    mine = mcp(env, BEA, ("add", {"what": "private matter", "due": "2h"}))[0]["value"]["id"]
    shared = mcp(env, BEA, ("add", {"what": "shared matter", "due": "2h", "shared_with": [DOV]}))[0]["value"]["id"]
    listed, owed, shown_mine, shown_shared = mcp(
        env, DOV, ("list", {}), ("owed", {}), ("show", {"id": mine}), ("show", {"id": shared}))
    assert [f["id"] for f in listed["value"]["followups"]] == [shared]
    assert owed["value"]["total"] == 1
    assert shown_mine["ok"] is False and shown_mine["value"]["refused"] == "absent"
    assert shown_shared["ok"] is True
    stranger = mcp(env, "@eli:local", ("list", {}), ("snooze", {"id": mine, "by": "9h"}), ("done", {"id": mine}))
    assert stranger[0]["value"]["count"] == 0 and not stranger[1]["ok"] and not stranger[2]["ok"]
    assert _sql(dsn, "SELECT state FROM program.followup WHERE id = %s", mine) == [("open",)]


def test_no_secret_value_is_accepted_stored_journaled_or_delivered(world):
    env, _, dsn = world
    refused = mcp(
        env, BEA,
        ("add", {"what": f"rotate using {SECRET}", "due": "1h"}),
        ("add", {"what": "ok", "due": "1h", "next_command": f"curl -H 'Authorization: {SECRET}'"}),
        ("add", {"what": "ok", "due": "1h", "check": "program.followup_owed", "check_args": {"api_key": "x"}}),
    )
    assert [r["ok"] for r in refused] == [False, False, False]
    assert all(SECRET not in json.dumps(r) for r in refused), "a refusal never echoes the value"
    assert _sql(dsn, "SELECT count(*) FROM program.followup") == [(0,)]
    ok = mcp(env, BEA, ("add", {"what": "fine", "due": "1h"}))[0]
    done = mcp(env, BEA, ("done", {"id": ok["value"]["id"], "note": f"found {SECRET}"}))[0]
    assert done["ok"] is False
    _no_secrets_anywhere(dsn)


def test_a_followup_that_came_due_while_nothing_ran_is_reported_missed(world):
    env, _, dsn = world
    fid = mcp(env, BEA, ("add", {"what": "check the verify result", "due": "1h"}))[0]["value"]["id"]
    _sql(dsn, "UPDATE program.followup SET due_at = now() - interval '30 minutes', "
              "next_action_at = now() - interval '30 minutes' WHERE id = %s", fid)  # no orchestrator has run
    owed = mcp(env, BEA, ("owed", {}))[0]["value"]
    assert [f["id"] for f in owed["missed"]] == [fid]
    assert owed["summary"].startswith("1 missed")


def test_status_leads_with_the_callers_due_and_open_followups(world):
    env, state, dsn = world
    (state / "program" / "data.json").write_text(json.dumps(GENERIC_PROGRAM))
    mcp(env, BEA, ("add", {"what": "approve the drop-old", "due": "2h", "next_command": "axi data drop-old orders_old"}))
    mine, theirs = mcp(env, BEA, ("status", {"scope": "schedule"})), mcp(env, DOV, ("status", {"scope": "schedule"}))
    assert mine[0]["ok"] and list(mine[0]["value"])[0] == "followups", "the follow-ups come first"
    block = mine[0]["value"]["followups"]
    assert block["principal"] == BEA and block["total"] == 1
    assert block["open"][0]["what"] == "approve the drop-old" and block["open"][0]["next_command"].startswith("axi data")
    assert theirs[0]["value"]["followups"] == {"principal": DOV, "total": 0, "summary": "nothing owed"}
    assert mine[0]["value"]["count"] > 0, "the status read itself is unchanged"


def test_the_cli_creates_and_lists_a_followup_for_the_operator(world):
    env, _, _ = world
    run = [sys.executable, "-m", "axiom.extensions.builtins.program.cli", "followup"]
    added = subprocess.run([*run, "add", "--what", "check the cli path", "--due", "2h", "--json"],
                           env=env, capture_output=True, text=True)
    assert added.returncode == 0, added.stderr
    fid = json.loads(added.stdout)["value"]["id"]
    owed = subprocess.run([*run, "owed", "--json"], env=env, capture_output=True, text=True)
    assert fid in owed.stdout
    done = subprocess.run([*run, "done", "--id", fid, "--note", "checked", "--json"], env=env, capture_output=True, text=True)
    assert json.loads(done.stdout)["value"]["state"] == "done"
