# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev logs``: the lane's node log (and its web server's), and ``-f`` follows them.

What `make dev-logs` did with a shell pipeline, as a verb over a skill (ADR-056): the logs of
the lane this checkout holds, the last lines of each, and with ``--follow`` every line written
after, until interrupted. It never claims a lane and never starts anything.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from axiom.extensions.builtins.dev import cli, node
from axiom.extensions.builtins.dev.skills import logs

LANE = {"name": "lane1", "front": 8898, "api": 8899, "database": "d", "isolated": True, "root": "/x"}


class Ctx:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir


@pytest.fixture
def lane_home(tmp_path, monkeypatch):
    """A lane this checkout holds, with a node log and a web log in its home."""
    monkeypatch.setattr(logs, "held_lane", lambda params: dict(LANE))
    state = tmp_path / "state"
    home = node.home_for(state, LANE["name"])
    home.mkdir(parents=True)
    node_log = home / "node.log"
    node_log.write_text("".join(f"node line {i}\n" for i in range(50)))
    (home / "node.json").write_text(json.dumps({"pid": 1, "url": "http://127.0.0.1:1/", "log": str(node_log)}))
    (home / "web.log").write_text("web line 0\n")
    monkeypatch.setattr(cli, "_state_dir", lambda: state)
    return state, home, node_log


def test_logs_names_each_log_and_its_last_lines(lane_home):
    state, home, node_log = lane_home
    r = logs.run({"n": 3}, Ctx(state))
    assert r.ok, r.errors
    by = {x["name"]: x for x in r.value["logs"]}
    assert by["node"]["path"] == str(node_log)
    assert by["node"]["lines"] == ["node line 47", "node line 48", "node line 49"]
    assert by["web"]["lines"] == ["web line 0"]
    assert by["node"]["end"] == node_log.stat().st_size


def test_lines_are_bounded_however_many_are_asked_for(lane_home):
    state, _home, _log = lane_home
    r = logs.run({"n": 10**9}, Ctx(state))
    assert len(r.value["logs"][0]["lines"]) <= logs.MAX_LINES


def test_after_an_offset_only_new_whole_lines_come_back(lane_home):
    state, _home, node_log = lane_home
    end = node_log.stat().st_size
    with open(node_log, "a") as f:
        f.write("fresh 1\nfresh 2\npartial")
    r = logs.run({"after": {"node": end}}, Ctx(state))
    got = {x["name"]: x for x in r.value["logs"]}["node"]
    assert got["lines"] == ["fresh 1", "fresh 2"]
    assert got["end"] == end + len("fresh 1\nfresh 2\n")


def test_a_lane_that_ran_nothing_says_how_to_start_it(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "held_lane", lambda params: dict(LANE))
    r = logs.run({}, Ctx(tmp_path / "state"))
    assert not r.ok and "dev up" in r.errors[0]


def test_no_lane_held_claims_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "held_lane", lambda params: None)
    r = logs.run({}, Ctx(tmp_path / "state"))
    assert not r.ok and "dev up" in r.errors[0]


def test_follow_prints_lines_written_after_it_starts_until_interrupted(lane_home, monkeypatch):
    _state, _home, node_log = lane_home
    polls = {"n": 0}

    def fake_sleep(_s):
        polls["n"] += 1
        if polls["n"] == 1:
            with open(node_log, "a") as f:
                f.write("written while following\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["logs", "-f", "--n", "2"])
    assert rc == 0, err.getvalue()
    text = out.getvalue()
    assert "node line 49" in text and "node line 47" not in text
    assert "written while following" in text
    assert str(node_log) in text


def test_without_follow_it_prints_once_and_exits(lane_home, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: pytest.fail("no follow, no polling"))
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(["logs", "--n", "1"])
    assert rc == 0 and "node line 49" in out.getvalue() and "web line 0" in out.getvalue()
