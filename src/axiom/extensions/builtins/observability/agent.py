# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ObservabilityAgent — service-home stub.

Today this is the install/diagnose loop's home. As the substrate
grows (Prometheus + Grafana siblings, eval-trigger automation) the
agent will own scheduled tasks. Keeping the seam in place per AEOS
"compound layout".
"""

from __future__ import annotations


class ObservabilityAgent:
    def __init__(self) -> None:
        self.name = "observability"

    def heartbeat(self) -> dict[str, str]:
        return {"name": self.name, "status": "alive"}
