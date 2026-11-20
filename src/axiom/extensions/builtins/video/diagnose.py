# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The diagnose ladder: the first rung that fails names its exact fix (VID-008).

Generic rungs first, then the kind's own (from its ``detect``), then a real frame:

``ffmpeg`` → ``numpy`` → ``declared`` → ``kind-registered`` → ``credential`` →
*the kind's detect rungs* (for ``rtsp``: ``url-resolves`` → ``describe-answers`` →
``auth-accepted`` → ``codec-decodable``) → ``verify``.

Every rung after the first failure is reported with ``ok = None`` (skipped), not
run against a source already known to be broken. Diagnose writes nothing; the
verify rung does not replace the recorded verify.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .naming import command as _command
from .connector import (
    ConnectorNotFound,
    NotAVideoConnector,
    connector_path,
    load,
    resolve_credential,
)
from .ffmpeg import FFmpegTool, install_hint
from .frame import EXTRA, numpy_available
from .sources import (
    InvalidSourceConfig,
    UnknownSourceKind,
    VideoSourceError,
    source_kind,
    source_kinds,
)

#: The generic rungs before the kind's, in order.
LEADING_STEPS = ("ffmpeg", "numpy", "declared", "kind-registered", "credential")
FINAL_STEP = "verify"
SKIPPED = "skipped: an earlier rung failed"


@dataclass(frozen=True)
class Step:
    step: str
    ok: bool | None  # None: skipped, because an earlier rung failed
    detail: str = ""
    fix: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _Ladder:
    def __init__(self) -> None:
        self.steps: list[Step] = []

    @property
    def failed(self) -> bool:
        return any(s.ok is False for s in self.steps)

    def rung(self, step: str, ok: bool | None, detail: str, fix: str = "") -> bool:
        if self.failed or ok is None:
            self.steps.append(Step(step, None, SKIPPED if self.failed else detail))
            return False
        self.steps.append(Step(step, ok, detail, "" if ok else fix))
        return ok

    def skip(self, *steps: str) -> None:
        for step in steps:
            self.steps.append(Step(step, None, SKIPPED))


def diagnose(name: str, *, state_dir: Path) -> dict[str, Any]:
    """``{name, ok, steps, fix}``: ``fix`` is the first failing rung's, or ``""``."""
    ladder = _Ladder()
    tool = FFmpegTool.locate()

    if tool.available:
        ladder.rung("ffmpeg", True, f"{tool.version()} ({tool.ffmpeg})")
    else:
        missing = " and ".join(
            n for n, p in (("ffmpeg", tool.ffmpeg), ("ffprobe", tool.ffprobe)) if not p
        )
        ladder.rung("ffmpeg", False, f"{missing} not found on this machine", install_hint())
    ladder.rung(
        "numpy",
        numpy_available(),
        "numpy is installed" if numpy_available() else "numpy is not installed",
        f"pip install '{EXTRA}'",
    )

    connector = None
    if not ladder.failed:
        try:
            connector = load(name, state_dir=state_dir)
            ladder.rung(
                "declared", True, f"declared at {connector_path(name, state_dir=state_dir)}"
            )
        except ConnectorNotFound as exc:
            ladder.rung(
                "declared",
                False,
                str(exc),
                f"declare it with `{_command()} add {name} --kind rtsp --url <rtsp://host/path> --credential-ref <ref>`",
            )
        except (NotAVideoConnector, InvalidSourceConfig) as exc:
            ladder.rung(
                "declared",
                False,
                str(exc),
                f"choose another name; `{_command()} list` shows the video sources",
            )
    else:
        ladder.skip("declared")

    kind = None
    if connector is not None and not ladder.failed:
        try:
            kind = source_kind(connector.source_kind)
            kind.validate(connector.source_params())
            ladder.rung("kind-registered", True, f"{kind.kind}: {kind.description or 'registered'}")
        except UnknownSourceKind:
            known = ", ".join(k.kind for k in source_kinds())
            ladder.rung(
                "kind-registered",
                False,
                f"no source kind {connector.source_kind!r} is registered",
                f"set kind under [connector.params.source] to a registered kind ({known}), or install the "
                f"package that provides {connector.source_kind!r}",
            )
        except InvalidSourceConfig as exc:
            ladder.rung("kind-registered", False, str(exc), exc.fix)
    else:
        ladder.skip("kind-registered")

    source = None
    if kind is not None and connector is not None and not ladder.failed:
        try:
            credential = resolve_credential(connector.credential_ref)
            source = kind.build(connector.source_params(), credential)
            ladder.rung(
                "credential",
                True,
                f"{connector.credential_ref} resolves"
                if connector.credential_ref
                else "no login is set (credential_ref is empty)",
            )
        except VideoSourceError as exc:
            ladder.rung("credential", False, str(exc), exc.fix)
    else:
        ladder.skip("credential")

    if source is not None and not ladder.failed:
        detected = source.detect()
        rungs = (detected.details or {}).get("steps") or []
        if rungs:
            for r in rungs:
                ladder.rung(r["step"], r["ok"], r.get("detail", ""), r.get("fix", ""))
        else:
            from axiom.extensions.builtins.connector.detect import ConnectorState

            ladder.rung(
                "detect",
                detected.state is ConnectorState.CONFIGURED,
                detected.summary,
                detected.next_action or "",
            )
    else:
        ladder.skip("detect")

    if source is not None and not ladder.failed:
        try:
            report = source.verify()
            rate = f" at {report.fps:g} fps" if report.fps else ""
            ladder.rung(
                FINAL_STEP,
                True,
                f"one frame: {report.codec} {report.width}x{report.height}{rate}, first frame in {report.latency_ms:.0f} ms",
            )
        except VideoSourceError as exc:
            ladder.rung(FINAL_STEP, False, str(exc), exc.fix)
    else:
        ladder.skip(FINAL_STEP)

    first = next((s for s in ladder.steps if s.ok is False), None)
    return {
        "name": name,
        "ok": first is None,
        "steps": [s.as_dict() for s in ladder.steps],
        "fix": first.fix if first else "",
    }


__all__ = ["FINAL_STEP", "LEADING_STEPS", "SKIPPED", "Step", "diagnose"]
