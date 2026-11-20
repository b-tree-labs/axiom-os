# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Video as instrument kind ``video``: ``<cli> instrument video <verb>`` (ADR-256 D4, spec-video §CLI).

The camera is the sensor; the video source declared on the node, and its capture,
is the instrument; what it quantifies into is frames. This object is all the
``instrument`` noun sees of video: a name, a line, what it yields, video's own
command line, and two summaries read from the same status view ``video.list`` and
``video.status`` read. Capture, the frame bus, triggers and the connector kind stay
video's (ADR-256 D6), and the skills keep their names (``video.add``; D5).

Declared in this extension's manifest as ``[[instrument_kinds]]``, so video reaches
the noun through the same gated door a consumer's kind uses. Importing this module
imports no video library.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping

from axiom.extensions.builtins.instrument.kinds import HEALTH_STATES, InstrumentRow, KindStatus


class VideoKind:
    """Instrument kind ``video`` (spec-instrument §``InstrumentKind``)."""

    name = "video"
    description = "Video sources as connectors (RTSP first): one capture session per source"
    yields = "frames"

    @property
    def skill_for_verb(self) -> Mapping[str, str]:
        """Every verb of ``instrument video``, and the ``video.<verb>`` skill it wraps."""
        from .commands import cli

        return {verb: cli.skill_for(verb) for verb in cli.VERBS}

    def build_parser(self, prog: str) -> argparse.ArgumentParser:
        from .commands import cli

        return cli.get_parser(prog)

    def main(self, argv: list[str], prog: str) -> int:
        from .commands import cli

        return cli.main(list(argv), prog=prog)

    def list(self) -> list[InstrumentRow]:
        """One row per declared video source: its health, capture state and source kind."""
        return [
            InstrumentRow(
                kind=self.name,
                name=str(row["name"]),
                health=str(row["health"]),
                state=str(row["state"]),
                detail=", ".join(
                    part for part in (str(row["kind"]), "started" if row["started"] else "") if part
                ),
            )
            for row in _rows()
        ]

    def status(self) -> KindStatus:
        """The sources counted by health, in one line, with the fix when one is not ready."""
        from .naming import command

        rows = _rows()
        counts = Counter(str(r["health"]) for r in rows)
        if not rows:
            return KindStatus(
                kind=self.name,
                ok=True,
                summary=f"no sources declared; `{command()} add` declares one",
            )
        n = len(rows)
        parts = [f"{n} source" + ("" if n == 1 else "s")]
        running = sum(1 for r in rows if r["state"] == "running")
        if running:
            parts.append(f"{running} running")
        parts += [f"{counts[h]} {h}" for h in HEALTH_STATES if counts.get(h)]
        unwell = n - counts.get("ready", 0)
        fix = (
            f"`{command()} status` shows each source; `{command()} diagnose <name>` names what is wrong"
            if unwell
            else None
        )
        return KindStatus(
            kind=self.name, ok=True, summary=", ".join(parts), counts=dict(counts), fix=fix
        )


def _rows() -> list[dict]:
    """The status view every video read uses (``video.list``, ``video.status``)."""
    from .skills._common import state_dir
    from .status import all_status

    return all_status(state_dir=state_dir(None))


#: What ``[[instrument_kinds]] name = "video"`` names.
KIND = VideoKind()

__all__ = ["KIND", "VideoKind"]
