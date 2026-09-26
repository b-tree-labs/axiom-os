# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Conversational review tools for neut chat.

Exposes the review framework to the chat agent so reviews can happen
conversationally — stream-of-consciousness feedback, confirmations,
visual progress, and interrupt tolerance.

Tools:
    review_start     — Start or resume a review session for a draft.
    review_get_item  — Fetch the next pending item for review.
    review_decide    — Record a decision (accept/edit/reject/skip) on an item.
    review_progress  — Show current session progress.
    review_complete  — Finalize the session and write approved output.
"""

from __future__ import annotations

from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.orchestrator.actions import ActionCategory
from axiom.review.adapters.draft_adapter import (
    create_draft_session,
    find_draft,
    write_approved_draft,
)
from axiom.review.models import (
    ReviewDecision,
    ReviewSession,
    ReviewSessionStore,
    _now_iso,
)

from ..tools import ToolDef

# Module-level store — lazily initialized
_store: ReviewSessionStore | None = None
_active_session: ReviewSession | None = None


def _get_store() -> ReviewSessionStore:
    global _store
    if _store is None:
        _store = ReviewSessionStore()
    return _store


def _get_session() -> ReviewSession | None:
    return _active_session


def _set_session(session: ReviewSession | None) -> None:
    global _active_session
    _active_session = session


# ── tool definitions ─────────────────────────────────────────────────

TOOLS = [
    ToolDef(
        name="review_start",
        description=(
            "Start or resume a review session for a draft document. "
            "Returns session info and the first pending item."
        ),
        category=ActionCategory.READ,
        parameters={
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": (
                        "Path to the draft file (relative to repo root or "
                        "absolute). If omitted, picks the most recent draft."
                    ),
                },
                "fresh": {
                    "type": "boolean",
                    "description": "Start a new session even if one exists for this file.",
                },
            },
        },
    ),
    ToolDef(
        name="review_get_item",
        description=(
            "Get the next pending review item, or a specific item by index. "
            "Returns the item heading, content, and index."
        ),
        category=ActionCategory.READ,
        parameters={
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": "0-based item index. If omitted, returns the next pending item.",
                },
            },
        },
    ),
    ToolDef(
        name="review_decide",
        description=(
            "Record a decision on a review item. Status can be: "
            "accepted, edited, rejected, or skipped. "
            "For 'edited', provide edited_content with the revised text."
        ),
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The item_id to decide on.",
                },
                "status": {
                    "type": "string",
                    "enum": ["accepted", "edited", "rejected", "skipped"],
                    "description": "The review decision.",
                },
                "edited_content": {
                    "type": "string",
                    "description": "Revised content (required when status is 'edited').",
                },
                "comment": {
                    "type": "string",
                    "description": "Optional comment explaining the decision.",
                },
                "reviewer": {
                    "type": "string",
                    "description": "Reviewer name (defaults to 'chat').",
                },
            },
            "required": ["item_id", "status"],
        },
    ),
    ToolDef(
        name="review_progress",
        description="Show progress on the active review session.",
        category=ActionCategory.READ,
        parameters={"type": "object", "properties": {}},
    ),
    ToolDef(
        name="review_complete",
        description=(
            "Finalize the review session: write the approved draft and "
            "register in publisher. Returns the path to the approved file."
        ),
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "approved_dir": {
                    "type": "string",
                    "description": "Override directory for approved output (relative to repo root).",
                },
            },
        },
    ),
]


# ── tool execution ───────────────────────────────────────────────────


def execute(name: str, params: dict) -> dict:
    """Dispatch tool calls to the appropriate handler."""
    handlers = {
        "review_start": _handle_start,
        "review_get_item": _handle_get_item,
        "review_decide": _handle_decide,
        "review_progress": _handle_progress,
        "review_complete": _handle_complete,
    }
    handler = handlers.get(name)
    if not handler:
        return {"error": f"Unknown review tool: {name}"}
    try:
        return handler(params)
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


def _handle_start(params: dict) -> dict:
    store = _get_store()
    file_arg = params.get("file")
    fresh = params.get("fresh", False)

    # Resolve draft path
    if file_arg:
        p = Path(file_arg)
        if not p.is_absolute():
            p = _REPO_ROOT / file_arg
        if not p.exists():
            # Try find_draft with it as a hint
            draft = find_draft(file_arg=str(p))
        else:
            draft = p
    else:
        draft = find_draft()

    if draft is None:
        return {
            "error": "No draft found. Provide a file path or place a draft in tools/agents/drafts/."
        }

    session = create_draft_session(draft, store, fresh=fresh)
    _set_session(session)

    reviewed, total = session.progress
    result: dict = {
        "session_id": session.session_id,
        "source": session.source,
        "total_items": total,
        "reviewed": reviewed,
        "pending": total - reviewed,
    }

    # Include first pending item
    pending = session.pending_items
    if pending:
        item = pending[0]
        idx = session.items.index(item)
        result["next_item"] = {
            "index": idx,
            "item_id": item.item_id,
            "heading": item.heading,
            "content": item.content,
            "context": item.context,
        }

    return result


def _handle_get_item(params: dict) -> dict:
    session = _get_session()
    if session is None:
        return {"error": "No active review session. Call review_start first."}

    index = params.get("index")
    if index is not None:
        if index < 0 or index >= len(session.items):
            return {"error": f"Index {index} out of range (0-{len(session.items) - 1})."}
        item = session.items[index]
    else:
        pending = session.pending_items
        if not pending:
            _reviewed, total = session.progress
            return {"message": f"All {total} items reviewed.", "complete": True}
        item = pending[0]
        index = session.items.index(item)

    return {
        "index": index,
        "total": len(session.items),
        "item_id": item.item_id,
        "heading": item.heading,
        "content": item.content,
        "context": item.context,
        "status": item.status,
    }


def _handle_decide(params: dict) -> dict:
    session = _get_session()
    if session is None:
        return {"error": "No active review session. Call review_start first."}

    item_id = params.get("item_id", "")
    status = params.get("status", "")
    edited_content = params.get("edited_content", "")
    comment = params.get("comment", "")
    reviewer = params.get("reviewer", "chat")

    if status not in ("accepted", "edited", "rejected", "skipped"):
        return {"error": f"Invalid status: {status}. Use accepted/edited/rejected/skipped."}

    if status == "edited" and not edited_content:
        return {"error": "edited_content is required when status is 'edited'."}

    # Find the item
    item = None
    for i in session.items:
        if i.item_id == item_id:
            item = i
            break

    if item is None:
        return {"error": f"Item '{item_id}' not found in session."}

    # Record decision
    decision = ReviewDecision(
        reviewer=reviewer,
        status=status,
        channel="chat",
        edited_content=edited_content,
        comment=comment,
        decided_at=_now_iso(),
    )
    item.decisions.append(decision)
    item.status = item.resolve_status(session.consensus_mode)
    session.last_reviewed_at = _now_iso()

    # Persist
    store = _get_store()
    store.save(session)

    reviewed, total = session.progress
    result: dict = {
        "item_id": item_id,
        "decision": status,
        "progress": f"{reviewed}/{total}",
    }

    # Include next pending item
    pending = session.pending_items
    if pending:
        next_item = pending[0]
        idx = session.items.index(next_item)
        result["next_item"] = {
            "index": idx,
            "item_id": next_item.item_id,
            "heading": next_item.heading,
            "content": next_item.content,
        }
    else:
        result["complete"] = True
        result["message"] = "All items reviewed. Call review_complete to finalize."

    return result


def _handle_progress(params: dict) -> dict:
    session = _get_session()
    if session is None:
        # Check store for any active sessions
        store = _get_store()
        active = store.list_active()
        if active:
            return {
                "message": "No session loaded in chat. Active sessions exist:",
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "source": s.source,
                        "progress": f"{s.progress[0]}/{s.progress[1]}",
                    }
                    for s in active
                ],
            }
        return {"message": "No active review sessions."}

    reviewed, total = session.progress
    items_summary = []
    for i, item in enumerate(session.items):
        items_summary.append(
            {
                "index": i,
                "heading": item.heading,
                "status": item.status,
            }
        )

    return {
        "session_id": session.session_id,
        "source": Path(session.source).name,
        "reviewed": reviewed,
        "total": total,
        "pending": total - reviewed,
        "items": items_summary,
    }


def _handle_complete(params: dict) -> dict:
    session = _get_session()
    if session is None:
        return {"error": "No active review session. Call review_start first."}

    approved_dir_str = params.get("approved_dir")
    approved_dir = None
    if approved_dir_str:
        approved_dir = _REPO_ROOT / approved_dir_str

    approved_path = write_approved_draft(session, approved_dir=approved_dir)

    if approved_path is None:
        return {"message": "All items were rejected. No approved draft written."}

    # Register in publisher
    try:
        from axiom.review.adapters.draft_adapter import _register_in_publisher

        _register_in_publisher(session, approved_path)
    except Exception:
        pass  # Non-fatal

    # Persist final state
    store = _get_store()
    store.save(session)

    _reviewed, total = session.progress
    accepted = sum(1 for i in session.items if i.status == "accepted")
    edited = sum(1 for i in session.items if i.status == "edited")
    rejected = sum(1 for i in session.items if i.status == "rejected")

    return {
        "approved_path": str(approved_path),
        "summary": {
            "accepted": accepted,
            "edited": edited,
            "rejected": rejected,
            "total": total,
        },
    }
