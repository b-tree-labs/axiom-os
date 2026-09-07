# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rail edit helper — Track 5.

Lets an instructor edit a rail's YAML directly, either via ``$EDITOR``
(CLI path) or by submitting the edited YAML text through a chat tool
/ Python API (AXI path).

Two public functions:

- ``load_rail_for_edit(course_id, rail_id)`` → YAML text the caller
  hands to an editor or prompt.
- ``apply_rail_edit(course_id, rail_id, new_yaml)`` → validates and
  persists the edit. Returns ``{applied: bool, error?: str}``.

The CLI layer composes these plus ``_launch_editor`` (monkey-patched
in tests) to drive the interactive flow.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def load_rail_for_edit(*, course_id: str, rail_id: str) -> str:
    """Return the rail's current YAML. Raises ValueError on lookup miss."""
    from .operational_store import load_course_data

    data = load_course_data(course_id)
    if data is None:
        raise ValueError(f"course {course_id!r} not found")

    rails = _rails_from_data(data)
    for r in rails:
        if r.get("id") == rail_id:
            return yaml.safe_dump(r, default_flow_style=False, sort_keys=False)

    known = [r.get("id") for r in rails]
    raise ValueError(f"rail {rail_id!r} not found; known: {known}")


def apply_rail_edit(
    *, course_id: str, rail_id: str, new_yaml: str,
) -> dict[str, Any]:
    """Validate ``new_yaml`` and persist it as the rail's new definition."""
    from .operational_store import _reg, load_course_data

    try:
        edited = yaml.safe_load(new_yaml)
    except yaml.YAMLError as e:
        return {"applied": False, "error": f"invalid yaml: {e}"}

    if not isinstance(edited, dict):
        return {
            "applied": False,
            "error": "edited rail must be a YAML mapping (dict)",
        }

    # Required fields: id, source, questions. Missing id/source is a
    # user error; a caller submitting an empty questions list is valid
    # (means "disable this rail's body without removing it") though
    # unusual.
    for required in ("id", "source"):
        if required not in edited:
            return {
                "applied": False,
                "error": f"edited rail missing required field: {required!r}",
            }

    if edited.get("id") != rail_id:
        return {
            "applied": False,
            "error": (
                f"cannot change rail id via edit "
                f"(original={rail_id!r}, edited={edited.get('id')!r}); "
                "remove the rail and add a new one instead"
            ),
        }

    data = load_course_data(course_id)
    if data is None:
        return {"applied": False, "error": f"course {course_id!r} not found"}

    manifest = dict(data.get("manifest") or {})
    rails = list(manifest.get("rails") or data.get("rails") or [])

    idx = next(
        (i for i, r in enumerate(rails) if r.get("id") == rail_id),
        None,
    )
    if idx is None:
        return {
            "applied": False,
            "error": f"rail {rail_id!r} not found on course {course_id!r}",
        }

    rails[idx] = edited
    manifest["rails"] = rails

    updated = dict(data)
    updated["manifest"] = manifest
    updated["rails"] = list(rails)
    _reg().register(kind="course", name=course_id, data=updated)

    return {"applied": True, "course_id": course_id, "rail_id": rail_id}


# ---------------------------------------------------------------------------
# Editor launcher — separate so tests can monkey-patch cleanly
# ---------------------------------------------------------------------------


def _launch_editor(path: str) -> int:
    """Invoke $EDITOR (or a fallback) on ``path``. Returns the exit code."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        proc = subprocess.run([editor, str(path)])
        return proc.returncode
    except FileNotFoundError:
        # Editor not installed — caller handles the non-zero return by
        # surfacing the error back to the user.
        return 127


def edit_rail_via_editor(
    *, course_id: str, rail_id: str,
) -> dict[str, Any]:
    """End-to-end CLI flow: load YAML → spawn editor → apply."""
    try:
        current = load_rail_for_edit(course_id=course_id, rail_id=rail_id)
    except ValueError as e:
        return {"applied": False, "error": str(e)}

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".rail.yaml", delete=False,
    ) as tmp:
        tmp.write(current)
        tmp_path = tmp.name

    try:
        rc = _launch_editor(tmp_path)
        if rc != 0 and rc != 127:
            return {"applied": False, "error": f"editor exited with {rc}"}
        edited = Path(tmp_path).read_text()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # If unchanged, short-circuit — persisting an identical YAML is a
    # no-op but bumps the artifact version chain needlessly.
    if edited == current:
        return {
            "applied": True,
            "course_id": course_id,
            "rail_id": rail_id,
            "noop": True,
        }

    return apply_rail_edit(
        course_id=course_id, rail_id=rail_id, new_yaml=edited,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _rails_from_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = data.get("manifest") or {}
    return list(manifest.get("rails") or data.get("rails") or [])
