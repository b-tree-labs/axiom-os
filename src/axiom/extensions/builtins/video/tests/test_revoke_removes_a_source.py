# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``instrument video revoke <name>``: the last step of the connector lifecycle (ADR-238 D5).

It stops capture, releases the claim, and removes the declaration and everything
recorded about the source. It leaves the login where it is: the vault owns it,
and another source may use it, so the result says it is still stored and how to
remove it. Undo is ``instrument video add`` again, so it is a reversible write.
"""

from __future__ import annotations

import os

import pytest

from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video import state as vs

from .harness import alive, decoder_pids, envelope, run_cli, wait_for


def _declare(state_dir, ref=None):
    vc.connect(
        vc.VideoConnector(
            name="cam-1", credential_ref=ref, source={"kind": "rtsp", "url": "rtsp://h/ok"}
        ),
        state_dir=state_dir,
    )


def test_revoke_removes_the_declaration_and_its_records(state_dir, call, fake_ffmpeg):
    _declare(state_dir)
    assert vc.verify("cam-1", state_dir=state_dir).ok
    vs.mark_started("cam-1", state_dir)
    result = call("video.revoke", name="cam-1")
    assert result.ok, result.errors
    assert result.value["revoked"] is True
    assert not vc.connector_path("cam-1", state_dir=state_dir).exists()
    assert vs.read_verify_record("cam-1", state_dir) is None
    assert not vs.is_started("cam-1", state_dir)
    assert any("cam-1" in a for a in result.actions_taken)
    left = [p.name for p in (state_dir / "video").rglob("cam-1*")]
    assert left == [], f"revoke leaves nothing of the source behind, lock files included: {left}"


def test_revoke_leaves_a_vault_login_and_says_how_to_remove_it(state_dir, call):
    _declare(state_dir, ref="foreign://video-cam-1")
    result = call("video.revoke", name="cam-1")
    assert result.ok
    assert "video-cam-1" in result.value["note"] and "vault rm video-cam-1" in result.value["note"]


def test_revoke_of_a_login_held_elsewhere_names_the_reference(state_dir, call):
    _declare(state_dir, ref="env://CAM_LOGIN")
    note = call("video.revoke", name="cam-1").value["note"]
    assert "env://CAM_LOGIN" in note and "vault rm" not in note


def test_revoke_of_a_source_nobody_declared_is_refused_with_the_list(state_dir, call):
    result = call("video.revoke", name="ghost")
    assert not result.ok
    assert "instrument video list" in " ".join(result.errors + [u["fix"] for u in result.unmet])


def test_revoke_is_a_reversible_write_that_needs_a_declared_source():
    from axiom.extensions.builtins.video.skills import SPECS

    spec = {s.name: s for s in SPECS}["video.revoke"]
    assert spec.risk == "reversible-write" and spec.surfaces == ("cli",)
    assert {p.id for p in spec.requires} == {"source-declared"}


@pytest.mark.skipif(os.name == "nt", reason="the stand-in tools are POSIX shell wrappers")
def test_revoking_a_running_source_stops_it_first(state_dir, fake_ffmpeg, no_leftovers):
    assert (
        run_cli(
            ["add", "cam-1", "--kind", "rtsp", "--url", "rtsp://cam.example.org/ok"],
            state_dir=state_dir,
        ).returncode
        == 0
    )
    started = envelope(run_cli(["start", "cam-1", "--json"], state_dir=state_dir))
    assert started["ok"]
    assert wait_for(lambda: decoder_pids(state_dir, "cam-1"), timeout=20)
    decoders = decoder_pids(state_dir, "cam-1")

    revoked = run_cli(["revoke", "cam-1", "--json"], state_dir=state_dir)
    assert revoked.returncode == 0, revoked.stdout + revoked.stderr
    body = envelope(revoked)["value"]
    assert body["was_running"] is True and body["revoked"] is True
    assert vs.claim_holder("cam-1", state_dir) is None
    assert wait_for(lambda: not any(alive(d) for d in decoders), timeout=10)
    assert "cam-1" not in run_cli(["list"], state_dir=state_dir).stdout


def test_revoke_keeps_the_history_and_closes_an_episode_left_open(state_dir, call):
    """The ledger is the record: revoking a source does not rewrite what it saw, and
    an episode a crashed capture left open is closed, not abandoned."""
    from axiom.extensions.builtins.video import ledger, runner
    from axiom.extensions.builtins.video.triggers import TriggerEvent

    _declare(state_dir)
    started = TriggerEvent(
        stream="cam-1",
        trigger="motion@0.1.0",
        ts="2026-11-10T12:00:00+00:00",
        kind="started",
        incident_key="cam-1:motion@0.1.0:e1",
    )
    ledger.append(started, state_dir)
    vs.write_open_episodes("cam-1", {started.incident_key: started.as_dict()}, state_dir)
    vs.request_reload("cam-1", state_dir)
    assert call("video.revoke", name="cam-1").ok
    rows = ledger.read(state_dir, stream="cam-1")
    assert [r["kind"] for r in rows] == ["started", "ended"]
    assert rows[1]["detail"]["reason"] == runner.REASON_REVOKED
    assert vs.read_open_episodes("cam-1", state_dir) == {}
    assert vs.read_reload_request("cam-1", state_dir) is None
