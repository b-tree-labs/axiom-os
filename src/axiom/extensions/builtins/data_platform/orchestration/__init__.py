# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Orchestration — wires source + bronze writer + RAG embedder into one
pass.

Pure Python; no Dagster dependency. The Dagster wrapper under
``data_platform/dagster/`` calls into this. PLINTH's ``run-ingest``
skill calls into this too. Same logic, two drivers.
"""

from __future__ import annotations

from .box_run import BoxRunReport, run_box_to_rag

__all__ = ["BoxRunReport", "run_box_to_rag"]
