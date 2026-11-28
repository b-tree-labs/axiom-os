# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""The public rivet-land-v1 tag follows the mirror, only onto a published pin."""

from __future__ import annotations

import importlib.util
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tagsync", ROOT / "scripts" / "rivet_land_tag_sync.py")
tagsync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tagsync)

ACTION = 'inputs:\n  config:\n    default: "x"\n  axiom-version:\n    description: "p"\n    required: false\n    default: "0.70.45"\n'
SHA = "b" * 40


def test_the_pin_is_read_from_the_real_action():
    real = (ROOT / "actions" / "rivet-land" / "action.yml").read_text()
    assert tagsync.pinned_version(real)
    assert tagsync.pinned_version(ACTION) == "0.70.45"


def _api(monkeypatch, *, on_pypi, tag_sha):
    calls = []

    def fake(url, token=None, method="GET", body=None):
        calls.append((method, url.rsplit("/", 1)[-1], body))
        if "pypi.org" in url:
            if not on_pypi:
                raise urllib.error.HTTPError(url, 404, "nf", {}, None)
            return {}
        if method == "GET":
            if tag_sha is None:
                raise urllib.error.HTTPError(url, 404, "nf", {}, None)
            return {"object": {"sha": tag_sha}}
        return {}

    monkeypatch.setattr(tagsync, "_call", fake)
    return calls


def test_the_tag_moves_onto_the_mirror_commit(monkeypatch):
    calls = _api(monkeypatch, on_pypi=True, tag_sha="a" * 40)
    assert "moved" in tagsync.sync(ACTION, "o/r", SHA, "t")
    assert ("PATCH", tagsync.TAG, {"sha": SHA, "force": True}) in calls


def test_a_current_tag_is_left_alone(monkeypatch):
    calls = _api(monkeypatch, on_pypi=True, tag_sha=SHA)
    assert "already" in tagsync.sync(ACTION, "o/r", SHA, "t")
    assert not [c for c in calls if c[0] in ("PATCH", "POST")]


def test_a_missing_tag_is_created(monkeypatch):
    calls = _api(monkeypatch, on_pypi=True, tag_sha=None)
    assert "created" in tagsync.sync(ACTION, "o/r", SHA, "t")
    assert any(c[0] == "POST" for c in calls)


def test_an_unpublished_pin_never_moves_the_tag(monkeypatch):
    calls = _api(monkeypatch, on_pypi=False, tag_sha="a" * 40)
    assert "stays" in tagsync.sync(ACTION, "o/r", SHA, "t")
    assert not [c for c in calls if c[0] in ("PATCH", "POST")]


def test_the_mirror_workflow_runs_the_sync_only_after_a_push():
    wf = (ROOT / ".github" / "workflows" / "mirror.yml").read_text()
    step = wf.split("Keep the rivet-land-v1 tag", 1)[1].split("- name:", 1)[0]
    assert "steps.push.outputs.pushed == 'true'" in wf.split("Keep the rivet-land-v1 tag", 1)[1]
    assert "rivet_land_tag_sync.py" in step
