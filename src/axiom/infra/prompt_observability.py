# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T0-3 prompt-composition observability writer.

Each chat turn emits one structured record describing which layers and
contributions made up the system prompt. Writes via CompositionService
when available (ideal — fragment is queryable and signed); falls back
to a per-session JSONL file so we never lose the record.

The JSONL fallback uses the exact same payload schema as the fragment
``content`` field, so the migration path is trivial once a chat-level
CompositionService is wired.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _runtime_root() -> Path:
    override = os.environ.get("AXIOM_RUNTIME_ROOT")
    if override:
        return Path(override)
    try:
        from axiom import REPO_ROOT  # type: ignore

        return Path(REPO_ROOT) / "runtime"
    except Exception:
        return Path.cwd() / "runtime"


def log_prompt_composition(
    payload: dict[str, Any],
    *,
    session_id: str,
    principal_id: str = "",
    composition=None,
) -> None:
    """Persist one prompt-composition observability record.

    Preferred path: ``composition.write(...)`` — emits a
    ``MemoryFragment(episodic)`` with ``fact_kind="prompt_composition"``.

    Fallback path: JSONL file at
    ``runtime/sessions/<session_id>/prompt_compositions.jsonl``.

    Always best-effort — never raises.
    """
    record = {
        "session_id": session_id,
        "principal_id": principal_id,
        "event_time": datetime.now(UTC).isoformat(),
        **payload,
    }

    # Preferred: write via CompositionService so the payload lands as a
    # signed episodic fragment queryable alongside other memory.
    if composition is not None:
        try:
            composition.write(
                content=record,
                cognitive_type="episodic",
                principal_id=principal_id or "axiom-system",
                agents={"neut-agent"},
                resources={f"session:{session_id}", "prompt-composition"},
            )
            return
        except Exception as exc:
            log.debug("composition write failed; falling back to JSONL: %s", exc)

    # Fallback: JSONL file. Schema parity with fragment.content so later
    # migration is a no-op.
    try:
        out_dir = _runtime_root() / "sessions" / (session_id or "unknown")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "prompt_compositions.jsonl"
        with out_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.debug("prompt-composition JSONL write failed: %s", exc)
