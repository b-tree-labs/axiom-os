# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Generic chat entry point — any terminal command can hand off into chat.

Usage:
    from axiom.extensions.builtins.chat.entry import enter_chat

    enter_chat(
        context_markdown="# Briefing\\n...",
        context_data=brief.to_dict(),
        title="Briefing: blockers",
        suggestions=["What are the key takeaways?"],
        source="neut_sense_brief",
    )

The chat session receives the context_markdown in its system prompt,
so the LLM can reference whatever the user just saw in the terminal.
"""

from __future__ import annotations

import sys
from typing import Any

from axiom.infra.bus import EventBus
from axiom.infra.gateway import Gateway
from axiom.infra.orchestrator.session import SessionStore

from .agent import ChatAgent
from .cli import run_repl
from .fullscreen import _SUGGESTIONS, FullScreenChat
from .provider_factory import create_input_provider, create_render_provider


def _format_briefing_context(briefing_data: dict) -> str:
    """Format a Briefing.to_dict() into readable markdown for the LLM.

    This is briefing-specific but lives here to avoid circular imports
    between sense and chat packages.
    """
    parts: list[str] = []
    parts.append("# Executive Briefing\n")

    topic = briefing_data.get("topic", "general")
    query = briefing_data.get("topic_query", "")
    if topic != "general":
        label = f"{topic}" + (f" ({query})" if query and query != topic else "")
        parts.append(f"**Topic:** {label}\n")

    tw_start = briefing_data.get("time_window_start", "")
    tw_end = briefing_data.get("time_window_end", "")
    if tw_start:
        parts.append(f"**Time window:** {tw_start} → {tw_end}")

    sig_count = briefing_data.get("signal_count", 0)
    parts.append(f"**Signals analyzed:** {sig_count}")

    by_type = briefing_data.get("signals_by_type", {})
    if by_type:
        breakdown = ", ".join(f"{v} {k}" for k, v in sorted(by_type.items(), key=lambda x: -x[1]))
        parts.append(f"**Breakdown:** {breakdown}")

    confidence = briefing_data.get("confidence", 0)
    parts.append(f"**Confidence:** {confidence:.0%}")

    summary = briefing_data.get("summary", "")
    if summary:
        parts.append(f"\n## Summary\n\n{summary}")

    key_signals = briefing_data.get("key_signals", [])
    if key_signals:
        parts.append("\n## Key Signals\n")
        for sig in key_signals[:10]:
            sig_type = sig.get("signal_type", "unknown")
            detail = sig.get("detail", sig.get("summary", ""))
            parts.append(f"- **[{sig_type}]** {detail}")

    return "\n".join(parts)


def enter_chat(
    context_markdown: str,
    context_data: dict[str, Any] | None = None,
    title: str = "",
    suggestions: list[str] | None = None,
    source: str = "",
) -> None:
    """Launch chat with pre-loaded context from a terminal command.

    This is the generic entry point — any command (briefing, sim results,
    log review, etc.) can call this to hand off into an interactive chat
    session with full context awareness.

    Args:
        context_markdown: Formatted context for the LLM system prompt.
        context_data: Raw structured data persisted in the session JSON.
        title: Session title (e.g., "Briefing: blockers").
        suggestions: Predictive placeholder texts for the input bar.
        source: Origin command identifier (e.g., "neut_sense_brief").
    """
    store = SessionStore()
    gateway = Gateway()
    bus = EventBus()

    # Build session context
    context: dict[str, Any] = {}
    if context_markdown:
        context["context_markdown"] = context_markdown
    if context_data:
        context["context_data"] = context_data
    if source:
        context["source"] = source

    session = store.create(context=context)
    if title:
        session.title = title

    agent = ChatAgent(gateway=gateway, bus=bus, session=session, render=None)

    # Inject custom suggestions if provided
    if suggestions:
        _SUGGESTIONS["context"] = suggestions

    # Offer an available update before the session starts, while there is
    # still a person looking at a plain terminal. Guarded to an interactive
    # TTY: a pipe, a serving surface or CI has nobody to answer, and a
    # question asked into one either hangs or is answered by accident.
    _maybe_offer_update(interactive=_is_tty())

    # Try fullscreen TUI first (same logic as neut chat)
    if _is_tty():
        try:
            tui = FullScreenChat(agent, store, stream=True, show_banner=False)
            if suggestions:
                tui._suggestion_key = "context"
            try:
                tui.run()
            finally:
                store.save(agent.session)
            return
        except Exception:
            pass  # Fall through to classic REPL

    # Classic REPL fallback
    render = create_render_provider()
    input_prov = create_input_provider()
    try:
        run_repl(agent, store, stream=True, render=render, input_prov=input_prov)
    finally:
        store.save(agent.session)


def _maybe_offer_update(*, interactive: bool) -> None:
    """Announce and offer a newer release, if there is one and someone to ask.

    Never raises and never blocks for long: a chat session that will not start
    because a version check timed out is a worse outcome than a missed update.
    """
    import os

    try:
        from axiom.extensions.builtins.update.offer import (
            is_version_skipped,
            offer_update_interactively,
            should_offer_update,
        )
        from axiom.extensions.builtins.update.version_check import VersionChecker
        from axiom.infra.branding import get_branding

        disabled = os.environ.get("AXIOM_DISABLE_UPDATE_NUDGE") == "1"
        if not interactive or disabled:
            return

        info = VersionChecker().check_remote_version(timeout=3.0)
        if not should_offer_update(
            is_newer=info.is_newer, interactive=interactive, disabled=disabled
        ):
            return
        available = info.available or ""
        if not available or is_version_skipped(available):
            return

        branding = get_branding()
        offer_update_interactively(
            product=branding.product_name,
            current=info.current,
            available=available,
            repo=os.environ.get("AXIOM_CONSUMER_RELEASE_REPO", "").replace(
                "https://github.com/", ""
            ),
            run_update=_run_update,
        )
    except Exception:  # noqa: BLE001 - never block a session from starting
        import logging

        logging.getLogger(__name__).debug("update offer skipped", exc_info=True)


def _run_update() -> None:
    """Hand off to the product's own update path."""
    from axiom.extensions.builtins.update.cli import main as update_main

    update_main([])


def _is_tty() -> bool:
    """Check if stdin/stdout are TTYs and prompt_toolkit is available."""
    if not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    try:
        import prompt_toolkit  # noqa: F401

        return True
    except ImportError:
        return False
