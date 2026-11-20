# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What ``ci_monitor.observe_pipelines`` returns for a list of statuses."""

from __future__ import annotations

from axiom.extensions.builtins.release.watch_integrity import BLIND, GREEN, RED, Observation


def seen_pipelines(pipelines: list) -> tuple[list, Observation]:
    if not pipelines:
        return pipelines, Observation(BLIND, ["nothing observed"])
    red = any(p.status != "success" for p in pipelines)
    return pipelines, Observation(RED if red else GREEN, pipelines_seen=len(pipelines))
