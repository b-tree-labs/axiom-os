# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Persistent, federation-aware background tasks.

A task is an in-flight piece of work the user (or the agent) spawned —
a long math computation, a multi-hour ingestion, an experiment that
takes 20 minutes. Unlike Claude Code's session-bound background tasks,
Axiom tasks:

  - Persist across CLI restarts (SQLite store under
    ``$AXI_STATE_DIR/tasks/``).
  - Carry a Matrix-style ``spawner_principal`` so peers can query each
    other's tasks once the federation peer-status path lands.
  - Stream stdout+stderr to a per-task file the user can tail with
    ``axi tasks tail <id>`` at any time, even after restarting the CLI.

Public surface:

  - :class:`Task` — frozen dataclass with the canonical fields.
  - :class:`TaskStore` — persistent store; create / get / update / list / clear.
  - :class:`TaskRunner` — subprocess management; spawn / status / cancel / tail.
"""

from .store import Task, TaskStatus, TaskStore

__all__ = ["Task", "TaskStatus", "TaskStore"]
