# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-240 D2: decoding is an ``ffmpeg`` subprocess; no video library is imported here.

A decoder bug or a codec hang in-process would take the service down with it, and
a native wheel would be pinned to every install. The only array library allowed
is numpy (the optional ``video`` extra), and only lazily: importing the extension
must not need it.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from .harness import SRC

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    "cv2",
    "av",
    "imageio",
    "imageio_ffmpeg",
    "PIL",
    "skimage",
    "moviepy",
    "decord",
    "ffmpeg",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _sources() -> list[Path]:
    return [p for p in ROOT.rglob("*.py") if "tests" not in p.relative_to(ROOT).parts]


def test_there_is_code_to_check():
    assert len(_sources()) >= 5


def test_no_module_imports_a_video_library():
    offenders = {str(p.relative_to(ROOT)): sorted(_imports(p) & FORBIDDEN) for p in _sources()}
    assert not {k: v for k, v in offenders.items() if v}


def test_the_guard_can_fail(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text("import cv2\nfrom av import open as av_open\n")
    assert _imports(sample) & FORBIDDEN == {"cv2", "av"}


def test_importing_the_extension_does_not_import_numpy():
    code = (
        "import sys; import axiom.extensions.builtins.video as v; "
        "import axiom.extensions.builtins.video.commands.cli; "
        "print('numpy' in sys.modules)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False"
