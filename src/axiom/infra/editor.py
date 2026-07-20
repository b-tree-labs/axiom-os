# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Open files or directories in the user's preferred editor.

Detection order: Cursor, VS Code, $EDITOR, skip.
Non-blocking — launches editor in background, never raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# Editors that can open directories (IDE-style)
_DIR_EDITORS = [
    ("Cursor", "cursor"),
    ("VS Code", "code"),
    ("PyCharm", "pycharm"),
    ("IntelliJ", "idea"),
]

# Editors that open files (terminal-style)
_FILE_EDITORS = [
    ("nvim", "nvim"),
    ("vim", "vim"),
]


def open_in_editor(path: str | Path, file: str | None = None) -> str | None:
    """Open a path in the user's preferred editor.

    Detection order:
    1. $VISUAL (GUI editors, standard on macOS/Linux)
    2. IDE-style editors that open directories (Cursor, VS Code, PyCharm)
    3. $EDITOR (terminal editors)
    4. Terminal editors on PATH (nvim, vim)

    Args:
        path: Directory or file to open.
        file: If path is a directory, optionally open this specific file within it.

    Returns:
        Name of the editor launched, or None if nothing was found.
    """
    target = str(path)
    file_target = str(Path(path) / file) if file else target

    # 1. $VISUAL — user's explicit GUI editor preference
    visual = os.environ.get("VISUAL")
    if visual and shutil.which(visual):
        if _launch([visual, target]):
            return visual

    # 2. IDE-style editors (open the directory for full project experience)
    for name, binary in _DIR_EDITORS:
        if shutil.which(binary):
            if _launch([binary, target]):
                return name

    # 3. $EDITOR — user's explicit terminal editor preference
    editor = os.environ.get("EDITOR")
    if editor and shutil.which(editor):
        if _launch([editor, file_target]):
            return editor

    # 4. Terminal editors on PATH
    for name, binary in _FILE_EDITORS:
        if shutil.which(binary):
            if _launch([binary, file_target]):
                return name

    return None


def _launch(cmd: list[str]) -> bool:
    """Launch a process in the background. Returns True on success."""
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False
