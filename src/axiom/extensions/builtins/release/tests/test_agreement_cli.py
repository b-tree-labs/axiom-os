# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi release agree``, ``agreement verify`` and ``agreement replay`` (#2471)."""

from __future__ import annotations

import json
from pathlib import Path

from axiom.extensions.builtins.release.agreement import cli, hook, store

from . import _agreement_helpers as h
from ._agreement_helpers import T0, minutes


def _logged(tmp_path: Path) -> str:
    rows = [
        h.assistant("Proposed: add retry handling to the sync client. Build it?", T0),
        h.human("build it", T0 + minutes(1)),
    ]
    path = h.write_transcript(tmp_path / "projects" / "p" / "S1.jsonl", rows)
    return hook.handle(h.hook_payload(rows[1], path), state_dir=tmp_path, now=T0 + minutes(1))


def test_agree_writes_a_record_for_a_logged_message(tmp_path: Path, capsys):
    entry_id = _logged(tmp_path)
    code = cli.main(
        [
            "agree",
            "--state-dir",
            str(tmp_path),
            "--repo",
            "o/n",
            "--pr",
            "7",
            "--head",
            "a" * 40,
            "--message",
            entry_id,
            "--work",
            "retry handling for the sync client",
            "--paths",
            "src/sync/,tests/test_client.py",
            "--action",
            "tag",
            "--format",
            "json",
        ]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["pr"] == 7 and printed["actions"] == ["tag"]
    assert store.read_records(tmp_path)[0].id == printed["id"]


def test_agree_refuses_a_message_the_hook_never_logged(tmp_path: Path, capsys):
    _logged(tmp_path)
    code = cli.main(
        [
            "agree",
            "--state-dir",
            str(tmp_path),
            "--repo",
            "o/n",
            "--pr",
            "7",
            "--head",
            "a" * 40,
            "--message",
            "deadbeefdeadbeef",
            "--work",
            "x",
            "--paths",
            "src/",
        ]
    )
    assert code != 0
    assert store.read_records(tmp_path) == []
    assert "not in the prompt log" in capsys.readouterr().err


def test_agree_has_no_way_to_pass_message_text():
    import argparse

    parser = cli.build_parser()
    flags = {
        a.dest
        for sub in parser._subparsers._actions
        if isinstance(sub, argparse._SubParsersAction)
        for p in sub.choices.values()
        for a in p._actions
    }
    assert not flags & {"text", "quote", "prompt", "body", "message_text"}


def test_axi_release_routes_agree_and_agreement_without_the_repository_guard(
    tmp_path: Path, capsys, monkeypatch
):
    from axiom.extensions.builtins.release import cli as release_cli

    monkeypatch.setattr(
        release_cli,
        "ensure_repo_or_offer_init",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("guard ran")),
    )
    entry_id = _logged(tmp_path)
    assert (
        release_cli.main(["agreement", "prompts", "--state-dir", str(tmp_path), "--format", "json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)[0]["id"] == entry_id
    code = release_cli.main(
        [
            "agree",
            "--state-dir",
            str(tmp_path),
            "--repo",
            "o/n",
            "--pr",
            "7",
            "--head",
            "a" * 40,
            "--message",
            entry_id,
            "--work",
            "retry handling",
            "--paths",
            "src/",
            "--format",
            "json",
        ]
    )
    assert code == 0 and store.read_records(tmp_path)[0].pr == 7


def test_verify_prints_the_verdict_for_a_pull_request(tmp_path: Path, capsys):
    entry_id = _logged(tmp_path)
    cli.main(
        [
            "agree",
            "--state-dir",
            str(tmp_path),
            "--repo",
            "o/n",
            "--pr",
            "7",
            "--head",
            "a" * 40,
            "--message",
            entry_id,
            "--work",
            "add retry handling to the sync client",
            "--paths",
            "src/sync/",
        ]
    )
    capsys.readouterr()
    code = cli.main(
        [
            "verify",
            "--state-dir",
            str(tmp_path),
            "--repo",
            "o/n",
            "--pr",
            "9",
            "--head",
            "a" * 40,
            "--files",
            "src/sync/a.py",
            "--format",
            "json",
        ]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["decision"] == "NOT_AGREED" and printed["reasons"] == ["no_record"]
