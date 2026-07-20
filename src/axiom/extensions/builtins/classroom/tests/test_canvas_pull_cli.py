# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end CLI test for `axi classroom canvas pull --fake`.

Pins the wiring: argparse → handler → CanvasLMSProvider → mock →
materials store. Uses the seeded fake Canvas in build_fake_canvas_for_cli
so the demo path stays visibly populated.
"""

from __future__ import annotations


def test_canvas_pull_fake_writes_materials(tmp_path, monkeypatch, capsys):
    from axiom.extensions.builtins.classroom.cli import main

    # Redirect HOME so the materials store lands under tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))

    rc = main([
        "canvas", "pull", "demo-classroom",
        "--fake",
        "--canvas-course-id", "c1",
    ])
    out = capsys.readouterr().out

    assert rc == 0
    # Summary visible in stdout
    assert "pages" in out
    assert "announcements" in out
    assert "files" in out

    # Materials store populated on disk
    base = (
        tmp_path / ".axi" / "coordinator" / "classrooms" / "demo-classroom"
    )
    materials = base / "materials"
    index = base / "materials_index.json"
    assert materials.exists()
    assert index.exists()

    files = sorted(p.name for p in materials.iterdir() if p.is_file())
    # Five entries from the seeded fake course (welcome.html, syllabus.html,
    # announcement-a1.html, lecture-1-slides.pdf, course-outline.md)
    assert len(files) == 5


def test_canvas_pull_live_requires_token(capsys):
    from axiom.extensions.builtins.classroom.cli import main

    rc = main([
        "canvas", "pull", "demo",
        "--canvas-course-id", "c1",
    ])
    (capsys.readouterr().err or "") + (capsys.readouterr().out or "")
    # Either an error message or non-zero exit signals the missing-token gate
    assert rc != 0


def test_canvas_pull_json_output(tmp_path, monkeypatch, capsys):
    import json

    from axiom.extensions.builtins.classroom.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))

    rc = main([
        "canvas", "pull", "demo-classroom",
        "--fake",
        "--canvas-course-id", "c1",
        "--json",
    ])
    out = capsys.readouterr().out

    assert rc == 0
    summary = json.loads(out)
    assert summary["pages"] == 2
    assert summary["announcements"] == 1
    assert summary["files"] == 1
    assert summary["outline"] == 1
    assert summary["total"] == 5
