# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The incident, reproduced against a real server, then the fixed control."""

from __future__ import annotations

import json
from dataclasses import replace

from axiom.extensions.builtins.synthetic.loader import parse_journey
from axiom.extensions.builtins.synthetic.runner import run_journey

from .conftest import JOURNEY, SESSION

ENV = {"SYNTH_COOKIE": SESSION}


def _run(base, env=ENV, journey=JOURNEY):
    return run_journey(replace(parse_journey(journey), base_url=base), env=env)


def _by_name(result):
    return {s.name: s for s in result.steps}


def test_the_incident_shell_with_no_cache_control_fails_the_revalidate_step(serve):
    base = serve(shell_cache_control=None)
    r = _run(base)
    assert r.status == "fail"
    step = _by_name(r)["signed-in page must be revalidated"]
    assert step.status == "fail"
    assert "heuristic" in step.detail and "without asking the server" in step.detail
    # the other two steps were fine: the gate itself was never the problem
    assert _by_name(r)["signed-out visitor is sent to sign in"].status == "pass"
    assert _by_name(r)["signed-out API call answers 401 JSON"].status == "pass"


def test_negative_control_the_fixed_server_passes(serve):
    r = _run(serve(shell_cache_control="no-cache"))
    assert r.status == "pass", [(s.name, s.detail) for s in r.steps]


def test_missing_session_cookie_is_unproven_never_pass(serve):
    r = _run(serve(shell_cache_control="no-cache"), env={})
    assert r.status == "unproven"
    step = _by_name(r)["signed-in page must be revalidated"]
    assert step.status == "unproven" and "SYNTH_COOKIE" in step.detail


def test_a_fail_outranks_unproven(serve):
    r = _run(serve(shell_cache_control=None), env={})
    # session step is unproven, but no other step fails here, so this is unproven...
    assert r.status == "unproven"
    j = json.loads(json.dumps(JOURNEY))
    j["step"][1]["expect"]["status"] = 200  # wrong on purpose
    assert _run(serve(shell_cache_control=None), env={}, journey=j).status == "fail"


def test_an_asset_served_as_html_is_caught(serve):
    # What a script bundle looks like when a gate answers it with the login page.
    r = _run(serve(shell_cache_control="no-cache", asset_is_html=True))
    step = _by_name(r)["signed-in page must be revalidated"]
    assert step.status == "fail"
    assert "/app.js" in step.detail and "instead of the asset itself" in step.detail


def test_an_unreachable_target_is_a_fail_with_the_reason_not_an_exception():
    r = _run("http://127.0.0.1:9")
    assert r.status == "fail"
    assert all(s.status in ("fail", "unproven") for s in r.steps)
    assert any("request failed" in s.detail for s in r.steps)


def test_cookie_and_set_cookie_values_are_never_recorded(serve):
    base = serve(shell_cache_control="no-cache")
    j = json.loads(json.dumps(JOURNEY))
    j["step"].append({"name": "login page", "path": "/gate/login", "expect": {"status": 200}})
    blob = json.dumps(_run(base, journey=j).to_dict()).lower()
    assert "secret-value" not in blob  # the Set-Cookie value the server sent
    assert "sid=good" not in blob  # the session cookie we sent
    assert "set-cookie" not in blob


def test_status_rolls_up_worst_first():
    from axiom.extensions.builtins.synthetic.model import roll_up

    assert roll_up(["pass", "pass"]) == "pass"
    assert roll_up(["pass", "unproven"]) == "unproven"
    assert roll_up(["unproven", "fail", "pass"]) == "fail"
    assert roll_up([]) == "unproven"
