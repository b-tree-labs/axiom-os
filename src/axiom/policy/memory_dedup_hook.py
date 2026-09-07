# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pre-write semantic dedup hook for memory-write tools (issue #202.3).

Wires onto `tool.pre_invoke` (the HookBus event fired by
`axiom.infra.tool_gateway.dispatch_tool`). For matching memory-write
tools, runs a similarity probe against recent fragments; if a
near-duplicate exists, returns a `deny` decision with a structured
payload the LLM can act on (existing fragment ID + similarity +
hint).

Design intent + tradeoffs: see
`docs/working/design-ext-import-and-tool-dispatch-dedup.md`.

Safety: any failure in the embedding probe → pass-through. This hook
is an optimization, never a hard dependency. Operator override via
`args.force=True` skips the probe entirely. Threshold tunable via
`AXIOM_MEMORY_DEDUP_THRESHOLD` env var.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)


# Default threshold tuned to "only obvious near-duplicates." Operators
# can lower to catch broader semantic overlap; raise to be more
# permissive. Values above ~0.95 trip on paraphrases too.
DEFAULT_DEDUP_THRESHOLD = 0.92


# Pattern: any tool name containing "memory" + "compose"/"write"/"add"
# (case-insensitive). Reads (retrieve / recall / search / get) are
# explicitly excluded — only writes need dedup.
_MEMORY_WRITE_PATTERN = re.compile(
    r"memory.*(compose|write|add|store|save)",
    re.IGNORECASE,
)
_MEMORY_READ_PATTERN = re.compile(
    r"memory.*(retrieve|recall|search|get|read|query)",
    re.IGNORECASE,
)


def is_memory_write_tool(tool_name: str) -> bool:
    """True iff this tool name looks like a memory-write op.

    Reads are excluded even when they match the broader pattern, so
    `memory_search` and `memory_retrieve` pass through cleanly.
    """
    if not tool_name:
        return False
    if _MEMORY_READ_PATTERN.search(tool_name):
        return False
    return bool(_MEMORY_WRITE_PATTERN.search(tool_name))


def _threshold() -> float:
    raw = os.environ.get("AXIOM_MEMORY_DEDUP_THRESHOLD")
    if raw is None:
        return DEFAULT_DEDUP_THRESHOLD
    try:
        return float(raw)
    except (ValueError, TypeError):
        log.warning(
            "AXIOM_MEMORY_DEDUP_THRESHOLD=%r is not a float; "
            "falling back to %s",
            raw, DEFAULT_DEDUP_THRESHOLD,
        )
        return DEFAULT_DEDUP_THRESHOLD


def _extract_content(args: dict[str, Any]) -> str:
    """Pull the write-content out of the args dict. Tries common keys
    (`content`, `text`, `body`); falls back to empty string."""
    for key in ("content", "text", "body", "fragment", "data"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _compute_max_similarity(content: str) -> tuple[float, str]:
    """Probe the recent-fragment corpus and return
    `(max_similarity, existing_fragment_id)`.

    Real impl (not in this slice) would use `rag.embeddings.embed_texts`
    + a cosine compare against the active session's fragments plus a
    bounded cross-session window (default last 100, env-tunable via
    `AXIOM_MEMORY_DEDUP_RECENT_N`). Stubbed to return `(0.0, "")` here;
    test seam — tests monkeypatch this function directly.

    Future-build TODO: wire to the actual fragment corpus + embedding
    store once the cross-session window selection logic is settled.
    See the design doc's "open design questions" section.
    """
    _ = content  # signature stable; impl deferred
    return (0.0, "")


def _build_deny_payload(
    *, similarity: float, existing_fragment_id: str,
) -> dict[str, Any]:
    """Structured deny payload — legible to an LLM so it can reference
    the existing fragment instead of trying to re-write it."""
    return {
        "deduped": True,
        "existing_fragment_id": existing_fragment_id,
        "similarity": similarity,
        "hint": (
            "This claim is already known to memory. Reference the "
            "existing fragment instead of re-writing it."
        ),
    }


def memory_dedup_pre_invoke(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Hook function for `tool.pre_invoke`. Return shape:

      - `None` to pass through (no decision)
      - `{"decision": "deny", "reason": str, "metadata": {...}}` to deny

    The HookBus contract treats `None` as no-op and a `decision`
    dict as authoritative.
    """
    tool_name = payload.get("tool_name", "") or ""
    if not is_memory_write_tool(tool_name):
        return None

    args = payload.get("args") or {}
    if not isinstance(args, dict):
        return None

    # Operator override: explicit force=True bypasses dedup.
    if args.get("force") is True:
        return None

    content = _extract_content(args)
    if not content:
        # Nothing to compare against — pass through. The tool itself
        # can decide whether an empty write is meaningful.
        return None

    try:
        max_sim, existing_id = _compute_max_similarity(content)
    except Exception as exc:
        # Probe failure must NEVER break dispatch. Log and pass through.
        log.debug("memory dedup probe failed: %s", exc)
        return None

    if max_sim >= _threshold():
        return {
            "decision": "deny",
            "reason": (
                f"semantic duplicate of fragment {existing_id} "
                f"(similarity={max_sim:.2f})"
            ),
            "metadata": _build_deny_payload(
                similarity=max_sim,
                existing_fragment_id=existing_id,
            ),
        }
    return None


# ---------------------------------------------------------------------------
# Platform adapter — `HookEntry` wrapper + default registration
# ---------------------------------------------------------------------------


def pre_invoke_handler(ctx):
    """Platform `HookEntry` — adapter from the pure pre-invoke fn to
    the `HookContext -> HookResult` signature the HookBus expects.

    Public so the memory extension's `axiom-extension.toml` can
    declare it as a `kind = "hook"` provider. HookRegistry discovers
    + registers it at startup; no imperative bootstrap needed.

    The structured deny metadata can't ride on `HookResult` today (no
    metadata field; see #202.3 follow-up to extend), so it gets
    JSON-encoded into the reason string. Callers that need structured
    access parse the `metadata=...` tail; the human-readable prefix
    stays clean.
    """
    import json

    from axiom.infra.hooks import allow, deny

    decision = memory_dedup_pre_invoke(ctx.payload)
    if decision is None:
        return allow()
    reason = decision.get("reason", "semantic duplicate")
    metadata = decision.get("metadata") or {}
    if metadata:
        reason = f"{reason}; metadata={json.dumps(metadata)}"
    return deny(reason=reason)
