# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.add``: declare a video source and verify it (ADR-238 D5 ``connect`` then ``verify``).

The connector is written first, then verified; a source that does not verify is
declared but inactive, and the result says why and how to fix it. A URL with a
login, an unknown kind or a taken name is refused before anything is written.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..connector import DEFAULT_FPS_CAP, VideoConnector, connect, connector_path, verify
from ..sources import InvalidSourceConfig, UnknownSourceKind, source_kind, source_kinds
from ._common import command, refuse, source_name, state_dir


def _fps_cap(raw: Any) -> Any:
    if raw is None or raw == "":
        return DEFAULT_FPS_CAP
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return raw


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    sd = state_dir(ctx)
    name = source_name(params)
    kind_name = str(params.get("kind") or "").strip()
    known = ", ".join(k.kind for k in source_kinds())
    if not name:
        return refuse("name the source", "pass a name like cam-1")
    if not kind_name:
        return refuse("name the source's kind", f"pass kind, one of: {known}")
    try:
        kind = source_kind(kind_name)
    except UnknownSourceKind as exc:
        return refuse(str(exc), f"pass kind, one of: {known}")

    source: dict[str, Any] = {
        "kind": kind.kind,
        **{k: v for k, v in dict(params.get("source") or {}).items() if k != "kind"},
    }
    for param in kind.params:
        given = params.get(param.name)
        if param.name not in source and given not in (None, ""):
            source[param.name] = given
        if param.name not in source and param.default is not None:
            source[param.name] = param.default

    connector = VideoConnector(
        name=name,
        source=source,
        credential_ref=str(params.get("credential_ref") or "").strip() or None,
        fps_cap=_fps_cap(params.get("fps_cap")),
    )
    try:
        path = connect(connector, state_dir=sd)
    except InvalidSourceConfig as exc:
        return refuse(str(exc), exc.fix.replace("<source name>", name))
    except UnknownSourceKind as exc:
        return refuse(str(exc), f"pass kind, one of: {known}")
    except FileExistsError as exc:
        return refuse(
            str(exc),
            f"choose another name, or edit {connector_path(name, state_dir=sd)} and run `{command()} verify {name}`",
        )

    record = verify(name, state_dir=sd)
    actions = [f"declared video source {name} ({kind.kind}) at {path}"]
    value = {
        "resource": "video_source",
        "name": name,
        "kind": kind.kind,
        "path": str(path),
        "verify": record.as_dict(),
        "active": record.ok,
    }
    if record.ok:
        report = record.report or {}
        actions.append(
            f"verified {name}: {report.get('codec')} {report.get('width')}x{report.get('height')}"
        )
        return SkillResult(ok=True, value=value, actions_taken=actions)
    return SkillResult(
        ok=False,
        value=value,
        errors=[
            f"declared {name}, but it did not verify, so it is not active: {record.error}. Fix: {record.fix}"
        ],
        actions_taken=actions,
    )


__all__ = ["run"]
