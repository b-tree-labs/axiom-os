# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Run the orchestrator as a process: ``python -m ...data_platform.orchestration``.

Under ``python -m axiom.infra.switch --worker -- <this>`` it is switched by
overlap with no lost or doubled firing (ADR-182 D3).
"""

from __future__ import annotations

import logging

from axiom.extensions.builtins.data_platform.orchestration.service import OrchestratorService

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    OrchestratorService().run_forever()
