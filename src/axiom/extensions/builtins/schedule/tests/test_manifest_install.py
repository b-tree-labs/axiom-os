# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The manifest-declared [[extension.schedule]] install hook (consumer seam §6).

Registration is idempotent by (extension, action) and fail-soft on bad input.
"""
from __future__ import annotations

import types
from pathlib import Path

from axiom.extensions.builtins.schedule import install


def _write_manifest(root: Path, *, action: str = "demo.tick", body: str | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    default = f'''
[extension]
name = "demo_ext"

[[extension.schedule]]
name = "nightly"
action = "{action}"
description = "demo cadence"

[extension.schedule.cadence]
kind = "interval"
interval_seconds = 3600
'''
    (root / "axiom-extension.toml").write_text(body if body is not None else default)


def _count(ext: str, action: str) -> int:
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with store.session_scope() as s:
        return s.query(ScheduleDefinition).filter_by(extension=ext, action=action).count()


def test_registers_declared_schedule(sqlite_store, tmp_path):
    _write_manifest(tmp_path)
    ext = types.SimpleNamespace(name="demo_ext", root=tmp_path)

    result = install.sync_manifest_schedules([ext])

    assert result.registered == ["demo_ext:demo.tick"]
    assert result.errors == []
    assert _count("demo_ext", "demo.tick") == 1


def test_registration_is_idempotent(sqlite_store, tmp_path):
    _write_manifest(tmp_path)
    ext = types.SimpleNamespace(name="demo_ext", root=tmp_path)

    first = install.sync_manifest_schedules([ext])
    second = install.sync_manifest_schedules([ext])

    assert first.registered == ["demo_ext:demo.tick"]
    assert second.registered == []
    assert second.skipped == ["demo_ext:demo.tick"]
    assert _count("demo_ext", "demo.tick") == 1  # not duplicated


def test_bad_block_is_captured_not_raised(sqlite_store, tmp_path):
    # schedule block missing the required 'action' key
    _write_manifest(
        tmp_path,
        body='''
[extension]
name = "demo_ext"

[[extension.schedule]]
name = "broken"

[extension.schedule.cadence]
kind = "interval"
interval_seconds = 60
''',
    )
    ext = types.SimpleNamespace(name="demo_ext", root=tmp_path)

    result = install.sync_manifest_schedules([ext])

    assert result.registered == []
    assert len(result.errors) == 1
    assert "action" in result.errors[0]


def test_no_schedule_blocks_is_noop(sqlite_store, tmp_path):
    (tmp_path / "axiom-extension.toml").write_text('[extension]\nname = "demo_ext"\n')
    ext = types.SimpleNamespace(name="demo_ext", root=tmp_path)

    result = install.sync_manifest_schedules([ext])

    assert result.registered == [] and result.skipped == [] and result.errors == []
