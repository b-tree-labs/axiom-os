# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The spawn points, the skills/CLI, chat /ps /stop and the TIDY check.

Same safety contract as ``test_process_containment``: only children this test
spawned are signalled, all of them carry a self-destruct deadline, and the
fixture asserts nothing it started survives.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from axiom.infra import process_registry as pr
from tests.infra.test_process_containment import SLEEP, Spawner, sp  # noqa: F401 - fixture reuse


def _wait(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------ spawn points


def test_task_runner_registers_marks_and_deregisters(sp, tmp_path, monkeypatch):  # noqa: F811
    from axiom.infra.tasks.runner import TaskRunner
    from axiom.infra.tasks.store import TaskStore

    monkeypatch.setenv("AXIOM_SESSION_ID", "sess-1")
    runner = TaskRunner(TaskStore(base_dir=tmp_path / "tasks"))
    code = "import os,time; print(os.environ.get('AXIOM_PROC_MARK','none'), flush=True); time.sleep(60)"
    task = runner.spawn(
        name="demo", command=[sys.executable, "-c", code], cwd=tmp_path, principal="@ben:local"
    )
    sp.track_pid(task.pid)
    recs = [r for r in pr.ProcessRegistry().records() if r.pid == task.pid]
    assert len(recs) == 1
    assert recs[0].kind == "task" and recs[0].detached and recs[0].session == "sess-1"
    assert recs[0].principal == "@ben:local"
    assert _wait(lambda: "axiom:" in task.output_path.read_text())  # child carries the mark
    runner.cancel(task.task_id)
    assert _wait(lambda: pr.probe(task.pid) is None)
    assert [r for r in pr.ProcessRegistry().records() if r.pid == task.pid] == []


def test_run_registered_returns_output_and_leaves_no_record(sp):  # noqa: F811
    res = pr.run_registered(
        [sys.executable, "-c", "print('hi')"],
        capture_output=True,
        registry=sp.registry,
        purpose="t",
    )
    assert res.returncode == 0 and res.stdout.strip() == b"hi"
    assert sp.registry.records() == []


def test_run_registered_is_visible_while_running(sp):  # noqa: F811
    seen = {}

    def probe_thread():
        _wait(lambda: sp.registry.records(), 5)
        seen["recs"] = sp.registry.records()

    import threading

    t = threading.Thread(target=probe_thread)
    t.start()
    pr.run_registered(
        [sys.executable, "-c", "import time; time.sleep(1.5)"],
        registry=sp.registry,
        purpose="visible",
        kind="tool",
    )
    t.join()
    assert [r.purpose for r in seen["recs"]] == ["visible"]
    assert seen["recs"][0].max_runtime_s is None  # no timeout given, none invented


def test_run_registered_timeout_kills_the_whole_group(sp):  # noqa: F811
    code = textwrap.dedent("""
        import subprocess, sys, time
        k = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        print(k.pid, flush=True)
        time.sleep(60)
    """)
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        pr.run_registered(
            [sys.executable, "-c", code],
            capture_output=True,
            timeout=1.5,
            new_session=True,
            registry=sp.registry,
            purpose="timeout",
        )
    grandchild = int((exc.value.output or b"0").split()[0])
    assert _wait(lambda: pr.probe(grandchild) is None), "grandchild must die with the group"
    assert sp.registry.records() == [], "record dropped after the timeout"


def test_dispatcher_heartbeat_runs_through_the_registry(sp, monkeypatch):  # noqa: F811
    from axiom.agents import background_service as bs

    seen = []
    real = pr.run_registered

    def spy(cmd, **kw):
        seen.append(kw)
        return real([sys.executable, "-c", "pass"], **{**kw, "registry": sp.registry})

    monkeypatch.setattr(pr, "run_registered", spy)
    res = bs._run_heartbeat(["axi", "tidy"], agent="tidy")
    assert res.returncode == 0
    assert seen[0]["agent"] == "tidy" and seen[0]["kind"] == "agent" and seen[0]["timeout"] == 120


def test_scidisplay_compute_worker_is_registered_and_grouped(sp, monkeypatch):  # noqa: F811
    import contextlib

    from axiom.extensions.builtins.scidisplay import compute as c

    seen = {}
    real = c.registered_process

    @contextlib.contextmanager
    def spy(proc, **kw):
        seen.update(kw)
        seen["leads_its_own_group"] = os.getpgid(proc.pid) == proc.pid
        with real(proc, **{**kw, "registry": sp.registry}) as rec:
            seen["registered_while_running"] = rec is not None and bool(sp.registry.records())
            yield rec

    monkeypatch.setattr(c, "registered_process", spy)
    c.compute(c.ComputationRequest(latex="1+1", mode="evaluate", timeout_seconds=30))
    assert seen["extension"] == "scidisplay" and seen["kind"] == "tool"
    assert seen["leads_its_own_group"] is True, "a timeout must be able to kill the worker's whole group"
    assert seen["registered_while_running"] is True
    assert sp.registry.records() == [], "record dropped once the worker exited"


# ------------------------------------------------------------ CLI / skills


def _cli(argv, capsys):
    from axiom.extensions.builtins.process import cli

    rc = cli.main(argv)
    return rc, capsys.readouterr().out


def test_cli_ls_show_stop_and_next_steps(sp, capsys, monkeypatch):  # noqa: F811
    p, rid = sp.spawn(SLEEP)
    sp.register(p, rid, purpose="cli demo")
    rc, out = _cli(["ls"], capsys)
    assert rc == 0 and rid in out and "cli demo" in out
    assert "Next:" in out and "process show <id>" in out
    rc, out = _cli(["show", rid], capsys)
    assert rc == 0 and "running" in out and f"process stop {rid}" in out
    rc, out = _cli(["stop", rid], capsys)  # default: dry-run
    assert rc == 0 and "dry-run" in out and "--kill" in out
    assert pr.probe(p.pid) is not None
    rc, out = _cli(["stop", rid, "--kill", "--grace", "3"], capsys)
    assert rc == 0 and "terminated" in out
    p.wait(timeout=5)
    assert _wait(lambda: pr.probe(p.pid) is None)


def test_cli_stop_unknown_id_fails_with_a_message(sp, capsys):  # noqa: F811
    rc, _ = _cli(["stop", "nope"], capsys)
    assert rc == 1


def test_cli_sweep_report_by_default_and_json(sp, capsys):  # noqa: F811
    p, rid = sp.spawn(SLEEP)
    sp.register(p, rid, max_runtime_s=0.2)
    time.sleep(0.8)
    rc, out = _cli(["--json", "sweep"], capsys)
    data = json.loads(out)["value"]
    assert data["mode"] == "report" and data["actions"] == []
    kinds = [f["kind"] for f in data["findings"]]
    assert "overrun" in kinds
    # Another session on this machine may carry a harness mark. That is not
    # this overrun, and report mode must not have acted on any of them.
    assert pr.probe(p.pid) is not None
    assert "process sweep --kill" in "\n".join(data["next"])
    rc, out = _cli(["sweep", "--kill"], capsys)
    p.wait(timeout=5)
    assert pr.probe(p.pid) is None and "terminated" in out


def test_cli_panic_dry_run_then_kill_and_resume(sp, capsys):  # noqa: F811
    from axiom.extensions.builtins.agents.consent import load_consent, record_decision

    record_decision(enabled=["alpha"])
    rc, out = _cli(["panic"], capsys)
    assert "dry-run" in out and "panic --kill" in out
    assert load_consent().enabled == ["alpha"]
    _cli(["panic", "--kill"], capsys)
    assert load_consent().enabled == []
    rc, out = _cli(["resume", "alpha", "--yes"], capsys)
    assert rc == 0 and load_consent().enabled == ["alpha"]
    rc, _ = _cli(["resume", "beta", "--yes"], capsys)
    assert rc == 1, "only agents containment paused can be resumed by it"


def test_skills_are_registered_via_the_registry_not_argparse():
    from axiom.extensions.builtins.process.skills import bind, verbs
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    assert all(reg.has(f"process.{v}") for v in verbs())
    assert sorted(verbs()) == ["ls", "panic", "resume", "show", "stop", "sweep"]


# ------------------------------------------------------------ chat


def _chat_agent(session_id):
    return SimpleNamespace(session=SimpleNamespace(session_id=session_id))


def test_chat_ps_lists_only_this_sessions_and_stop_refuses_strangers(sp):  # noqa: F811
    from axiom.extensions.builtins.chat.process_commands import cmd_ps, cmd_stop

    mine, rm = sp.spawn(SLEEP)
    sp.register(mine, rm, session="s1", purpose="mine")
    theirs, rt = sp.spawn(SLEEP)
    sp.register(theirs, rt, session="s2", purpose="theirs")
    # the chat helpers read the default registry: point it at the test state
    out = cmd_ps(_chat_agent("s1"))
    assert rm in out and "mine" in out and rt not in out
    assert "1 more owned by other" in out and "Next:" in out and "/stop <id>" in out
    refused = cmd_stop(_chat_agent("s1"), [rt])
    assert "not started by this session" in refused and pr.probe(theirs.pid) is not None
    done = cmd_stop(_chat_agent("s1"), [rm])
    assert "terminated" in done or "killed" in done
    mine.wait(timeout=5)
    assert _wait(lambda: pr.probe(mine.pid) is None)
    assert pr.probe(theirs.pid) is not None
    assert "Usage" in cmd_stop(_chat_agent("s1"), [])


def test_chat_registers_ps_and_stop_as_commands():
    from axiom.extensions.builtins.chat.commands import CHAT_META_COMMANDS

    assert "/ps" in CHAT_META_COMMANDS and "/stop" in CHAT_META_COMMANDS


# ------------------------------------------------------------ TIDY


def test_tidy_node_health_check_reports_and_is_silent_when_clean(sp):  # noqa: F811
    from axiom.extensions.builtins.hygiene.node_health import check_rogue_processes

    assert check_rogue_processes() is None, "nothing ever registered: no finding, no ps"
    p, rid = sp.spawn(SLEEP)
    sp.register(p, rid, max_runtime_s=0.2)
    time.sleep(0.8)
    f = check_rogue_processes()
    assert f is not None and f.check == "rogue_processes"
    assert "process sweep" in f.message and "nothing has been stopped" in f.message
    assert pr.probe(p.pid) is not None, "the TIDY check never signals"
    only_info = SimpleNamespace(findings=[SimpleNamespace(kind="stale_record")])
    assert check_rogue_processes(report=only_info) is None
