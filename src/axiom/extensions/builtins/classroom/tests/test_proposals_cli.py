# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end CLI tests for `axi classroom proposals` (Phase 0.2).

Subcommands: create, list, approve, reject, push.

Push uses the same `--fake` mock path as `axi classroom canvas pull`
so demos work offline. Live LMS push is exercised by separate
integration tests.
"""

from __future__ import annotations

import json


def test_create_then_list(tmp_path, monkeypatch, capsys):
    from axiom.extensions.builtins.classroom.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))

    rc = main([
        "proposals", "create", "ne101",
        "--target", "page",
        "--action", "create",
        "--title", "Welcome",
        "--body", "<h1>Welcome</h1>",
        "--created-by", "instructor:ondrej",
    ])
    assert rc == 0
    create_out = capsys.readouterr().out
    assert "draft" in create_out.lower()

    rc = main(["proposals", "list", "ne101", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["title"] == "Welcome"
    assert rows[0]["status"] == "draft"


def test_approve_reject_workflow(tmp_path, monkeypatch, capsys):
    from axiom.extensions.builtins.classroom.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))

    main([
        "proposals", "create", "ne101",
        "--target", "page", "--action", "create",
        "--title", "A", "--body", "x",
        "--created-by", "i",
    ])
    main([
        "proposals", "create", "ne101",
        "--target", "page", "--action", "create",
        "--title", "B", "--body", "x",
        "--created-by", "i",
    ])
    capsys.readouterr()

    main(["proposals", "list", "ne101", "--json"])
    rows = json.loads(capsys.readouterr().out)
    pid_a = next(r["proposal_id"] for r in rows if r["title"] == "A")
    pid_b = next(r["proposal_id"] for r in rows if r["title"] == "B")

    rc = main([
        "proposals", "approve", pid_a, "--by", "instructor:ondrej",
    ])
    assert rc == 0
    capsys.readouterr()  # drop approve narration

    rc = main([
        "proposals", "reject", pid_b,
        "--reason", "off-topic", "--by", "instructor:ondrej",
    ])
    assert rc == 0
    capsys.readouterr()  # drop reject narration

    main(["proposals", "list", "ne101", "--json"])
    rows = json.loads(capsys.readouterr().out)
    by_title = {r["title"]: r["status"] for r in rows}
    assert by_title["A"] == "approved"
    assert by_title["B"] == "rejected"


def test_push_fake_runs_through_canvas_mock(tmp_path, monkeypatch, capsys):
    from axiom.extensions.builtins.classroom.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))

    # Create a page-create proposal and approve it.
    main([
        "proposals", "create", "ne101",
        "--target", "page", "--action", "create",
        "--title", "Welcome", "--body", "<h1>Welcome</h1>",
        "--created-by", "instructor:ondrej",
    ])
    capsys.readouterr()

    main(["proposals", "list", "ne101", "--json"])
    rows = json.loads(capsys.readouterr().out)
    pid = rows[0]["proposal_id"]

    main(["proposals", "approve", pid, "--by", "instructor:ondrej"])
    capsys.readouterr()

    rc = main([
        "proposals", "push", pid,
        "--fake",
        "--canvas-course-id", "c1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pushed" in out.lower()

    # Status now "pushed"
    main(["proposals", "list", "ne101", "--json"])
    after = json.loads(capsys.readouterr().out)
    assert after[0]["status"] == "pushed"
    assert after[0]["pushed_lms_id"]


def test_push_unapproved_proposal_fails(tmp_path, monkeypatch, capsys):
    from axiom.extensions.builtins.classroom.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))

    main([
        "proposals", "create", "ne101",
        "--target", "page", "--action", "create",
        "--title", "x", "--body", "b",
        "--created-by", "i",
    ])
    capsys.readouterr()
    main(["proposals", "list", "ne101", "--json"])
    pid = json.loads(capsys.readouterr().out)[0]["proposal_id"]

    rc = main([
        "proposals", "push", pid,
        "--fake",
        "--canvas-course-id", "c1",
    ])
    assert rc != 0
