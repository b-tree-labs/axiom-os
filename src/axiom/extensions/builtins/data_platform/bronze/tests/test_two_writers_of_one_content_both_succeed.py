# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Two writers of the same content both succeed (#1710, defect 1).

Seen on a hosted node: when a producer re-sent after a server stall, two
requests wrote the same content hash at once. Both used ``<sha>.part`` as the
temporary name; the first renamed it into place and the second's rename failed
with "No such file or directory". The content was there, so nothing was lost,
but the push returned an error and logged it.

Each writer now writes its own temporary file, and finding the final file
already in place is success.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.bronze.sinks import (
    FilesystemBronzeSink,
    FilesystemTabularBronzeSink,
)

CONTENT = b'[{"t": 1, "v": 2.5}]'
HASH = hashlib.sha256(CONTENT).hexdigest()


def _writer(tmp_path: Path, kind: str):
    if kind == "files":
        sink = FilesystemBronzeSink(root=tmp_path)
        return lambda: sink.write_content(content=CONTENT, content_hash=HASH)
    sink = FilesystemTabularBronzeSink(root=tmp_path)
    return lambda: sink.write_content(content=CONTENT, content_hash=HASH, source_name="example-feed")


@pytest.mark.parametrize("kind", ["files", "tabular"])
def test_a_second_writer_landing_first_does_not_fail_the_first(tmp_path, monkeypatch, kind):
    write = _writer(tmp_path, kind)
    real_replace = Path.replace
    state = {"raced": False}

    def replace(self, target):
        # The first writer has written its temporary file and is about to rename
        # it. A second writer of the same content runs to completion first.
        if not state["raced"]:
            state["raced"] = True
            write()
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    blob = write()

    assert state["raced"]
    assert blob.read_bytes() == CONTENT
    assert not list(blob.parent.glob("*.part")), "no temporary file is left behind"


@pytest.mark.parametrize("kind", ["files", "tabular"])
def test_finding_the_final_file_already_there_is_success(tmp_path, monkeypatch, kind):
    write = _writer(tmp_path, kind)
    blob = write()

    def vanished(self, target):
        # Another writer's rename won, and this writer's temporary file is gone.
        self.unlink()
        raise FileNotFoundError(str(self))

    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr(Path, "replace", vanished)
    assert write() == blob
    assert blob.read_bytes() == CONTENT
