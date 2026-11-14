# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Triggers that do exactly what a test says, for the runner and discovery tests.

``ScriptedTrigger`` is what a consumer's package would ship: a ``BaseTrigger``
built from its config alone. Its config says what to emit at which ``seq``
(``{"script": {"3": [["started", "e1"]], "9": [["ended", "e1"]]}}``), which
``seq`` raises, and how slow it is. Every instance is kept in :data:`BUILT` so
a test can see the config a trigger was given and the frames it saw.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from axiom.extensions.builtins.video.triggers import BaseTrigger, TriggerConfigError

#: Every ScriptedTrigger built, in order.
BUILT: list[ScriptedTrigger] = []


class ScriptedTrigger(BaseTrigger):
    name = "scripted"
    version = "1.0.0"
    description = "a test double that emits what its config says"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        config = dict(config or {})
        self.script = {
            int(k): [tuple(e) for e in v] for k, v in (config.pop("script", {}) or {}).items()
        }
        self.raise_at = {int(s) for s in config.pop("raise_at", ())}
        self.config_error_at = {int(s) for s in config.pop("config_error_at", ())}
        self.sleep_s = float(config.pop("sleep_s", 0.0))
        self.stream_override = config.pop("stream_override", None)
        super().__init__(config)
        self.seen: list[Any] = []
        self.started = self.stopped = False
        BUILT.append(self)

    def start(self) -> None:
        self.started = True

    def observe(self, frame):
        self.seen.append(frame)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if frame.seq in self.config_error_at:
            raise TriggerConfigError("the region is outside the frame", fix="set a smaller region")
        if frame.seq in self.raise_at:
            raise RuntimeError(f"scripted failure at {frame.seq}")
        out = []
        for kind, episode in self.script.get(frame.seq, ()):
            event = self.event(frame, kind, episode, score=0.5)
            if self.stream_override:
                from dataclasses import replace

                event = replace(
                    event,
                    stream=self.stream_override,
                    incident_key=f"{self.stream_override}:{event.trigger}:{episode}",
                )
            out.append(event)
        return out

    def health(self) -> dict[str, Any]:
        return {"seen": len(self.seen)}

    def stop(self) -> None:
        self.stopped = True


def build(config: Mapping[str, Any]) -> ScriptedTrigger:
    """The entry-point shape: ``build(config) -> StreamTrigger``."""
    return ScriptedTrigger(config)


__all__ = ["BUILT", "ScriptedTrigger", "build"]
