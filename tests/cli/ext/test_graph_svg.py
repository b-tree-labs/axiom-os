# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext graph --svg`` / ``--render``.

We avoid requiring the real mmdc binary in CI: the success test is skipped
when ``mmdc`` is absent and the missing-binary error is still exercised.
"""

from __future__ import annotations

import argparse
import stat
from pathlib import Path

import pytest

from axiom.cli.ext.commands.graph import GraphProvider
from axiom.cli.ext.provider import CliContext


def _run(*argv: str, cwd: Path, capsys) -> tuple[int, str, str]:
    provider = GraphProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(list(argv))
    ctx = CliContext(cwd=cwd)
    capsys.readouterr()
    rc = provider.run(args, ctx)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# ---------------------------------------------------------------------------
# Missing-mmdc error
# ---------------------------------------------------------------------------


def test_svg_missing_mmdc_hints_install(
    scaffolded_extension, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    ext = scaffolded_extension("g_ext")
    # Force an empty PATH so mmdc is not found.
    monkeypatch.setenv("PATH", "")
    svg_path = tmp_path / "out.svg"
    rc, out, err = _run(str(ext), "--svg", str(svg_path), cwd=tmp_path, capsys=capsys)
    assert rc == 2
    combined = (out + err).lower()
    assert "mmdc" in combined
    assert "install" in combined


# ---------------------------------------------------------------------------
# --svg happy path via fake mmdc shim
# ---------------------------------------------------------------------------


def test_svg_writes_via_mmdc_shim(
    scaffolded_extension, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    ext = scaffolded_extension("g_ext")
    # Set up a fake mmdc wrapper that just writes a placeholder SVG.
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "mmdc"
    shim.write_text(
        "#!/bin/sh\n"
        "out=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  case $1 in\n"
        "    -o) out=$2; shift 2;;\n"
        "    *) shift;;\n"
        "  esac\n"
        "done\n"
        'echo "<svg>ok</svg>" > "$out"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(shim_dir))

    svg_path = tmp_path / "out.svg"
    rc, out, err = _run(
        str(ext), "--svg", str(svg_path), cwd=tmp_path, capsys=capsys
    )
    assert rc == 0, out + err
    assert svg_path.exists()
    assert "<svg>" in svg_path.read_text()


# ---------------------------------------------------------------------------
# --render
# ---------------------------------------------------------------------------


def test_render_invokes_opener(
    scaffolded_extension, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    ext = scaffolded_extension("g_ext")
    # mmdc shim as above, so the SVG gets created.
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "mmdc"
    shim.write_text(
        "#!/bin/sh\n"
        "out=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  case $1 in\n"
        "    -o) out=$2; shift 2;;\n"
        "    *) shift;;\n"
        "  esac\n"
        "done\n"
        'echo "<svg/>" > "$out"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(shim_dir))

    # Capture the open invocation.
    import axiom.cli.ext.commands.graph as graph_mod

    calls: list[tuple] = []

    def fake_open(cmd, **kw):
        calls.append((cmd, kw))
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(graph_mod.subprocess, "run", fake_open)
    rc, out, err = _run(str(ext), "--render", cwd=tmp_path, capsys=capsys)
    assert rc == 0, out + err
    # One opener call should have fired for the SVG.
    assert calls, f"expected opener invocation, got none; stdout={out!r}"
    cmd = calls[-1][0]
    # Either `open <path>` (darwin), `xdg-open <path>` (linux), or print-only
    # on Windows — the first-arg check is platform-flexible.
    assert any(".svg" in str(x) for x in cmd)
