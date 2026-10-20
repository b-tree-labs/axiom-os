# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The node knows itself — self-knowledge, fresh and fast.

Founder requirements (2026-09-24, after the chat waffled on "am I
federated with UT's TRIGA?"):

1. **Wicked fast.** The common self-facts must be answerable in ONE
   turn with zero tool calls — so :func:`node_summary` is cheap enough
   (file/registry reads only; never a database or network touch) to
   ride the system prompt on every turn, and it is cached for a few
   seconds so a busy surface never pays twice. The deep answers live
   behind :func:`node_profile` (the ``node_describe`` tool), still
   registry-derived at call time — never a cached description that can
   go stale.
2. **First person.** The agent IS this node's agent: it says "I'm not
   federated with any site right now", never "the system would
   typically". The prompt fragment below carries both the voice rule
   and the fresh facts.
"""

from __future__ import annotations

import os
import time
from typing import Any

_SUMMARY_TTL_SECONDS = 5.0
_summary_cache: tuple[float, dict] | None = None

# Section derivations cost real work (the chat-tool scan walks every
# installed extension; the gate section reads provider config), so they
# carry the same short TTL as the summary. This is a CACHE OF A
# DERIVATION, not a stored description: the first call after anything
# changes re-derives, and nothing here is ever written down.
_SECTION_TTL_SECONDS = 5.0
_section_cache: dict[str, tuple[float, Any]] = {}


# ------------------------------------------------------------------ helpers
def _version() -> dict[str, Any]:
    """Version AND where this process's code actually came from — on a dev
    box the installed distribution and the imported source diverge, and an
    agent that reports only the former misdescribes itself."""
    import axiom

    out: dict[str, Any] = {"source_path": str(getattr(axiom, "__file__", "?"))}
    try:
        from importlib.metadata import version

        out["distribution"] = version("axiom-os-lm")
    except Exception:  # noqa: BLE001 — editable/dev installs
        out["distribution"] = getattr(axiom, "__version__", "unknown")
    return out


def _extensions() -> list[str]:
    from pathlib import Path

    import axiom.extensions.builtins as b

    root = Path(b.__file__).parent
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and (p / "axiom-extension.toml").is_file()
    )


def _federation() -> dict[str, Any]:
    """Local federation state from the node registry + identity files.
    File reads only; an unconfigured node answers honestly and fast."""
    out: dict[str, Any] = {"configured": False, "peers": [], "identity": None}
    try:
        from axiom.vega.federation.discovery import NodeRegistry

        registry = NodeRegistry()
        try:
            registry.load()
        except Exception:  # noqa: BLE001 — no registry file = no peers
            pass
        peers = registry.list_all() or []
        # A human asked "am I federated with X" — answer with the name a
        # human uses, not the opaque node id (which is kept alongside).
        out["peers"] = [
            {
                "name": getattr(n, "display_name", None) or getattr(n, "node_id", "?"),
                "node_id": getattr(n, "node_id", "?"),
                "url": getattr(n, "url", None),
            }
            for n in peers
        ]
        out["configured"] = bool(peers)
    except Exception:  # noqa: BLE001
        pass
    try:
        from axiom.vega.federation.identity import load_identity

        identity = load_identity()
        if identity is not None:
            out["identity"] = {
                "node_id": identity.node_id,
                "owner": getattr(identity, "owner", None),
            }
    except Exception:  # noqa: BLE001
        pass
    return out


def _site() -> str | None:
    return os.environ.get("AXIOM_SITE") or None


# ------------------------------------------------------------------ summary
def node_summary(*, now: float | None = None) -> dict[str, Any]:
    """The prompt-riding facts: cheap by construction (files + registries,
    no DB, no network), cached ~5s. This is what makes self-questions
    one-turn fast."""
    global _summary_cache
    t = time.monotonic() if now is None else now
    if _summary_cache is not None and t - _summary_cache[0] < _SUMMARY_TTL_SECONDS:
        return _summary_cache[1]

    fed = _federation()
    summary = {
        "version": _version(),
        "site": _site(),
        "extensions": _extensions(),
        "federation": fed,
    }
    _summary_cache = (t, summary)
    return summary


def reset_summary_cache() -> None:
    global _summary_cache
    _summary_cache = None
    _section_cache.clear()


# ------------------------------------------------------------------ profile
def node_profile(section: str | None = None) -> dict[str, Any]:
    """The deep answers, derived live per call. Each section is guarded:
    an unavailable subsystem reports itself unavailable rather than
    sinking the whole answer."""
    sections: dict[str, Any] = {}

    now = time.monotonic()

    def _add(name: str, fn) -> None:
        if section is not None and name != section:
            return
        cached = _section_cache.get(name)
        if cached is not None and now - cached[0] < _SECTION_TTL_SECONDS:
            sections[name] = cached[1]
            return
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 — honest partial answers
            value = {"unavailable": str(exc)[:200]}
        _section_cache[name] = (now, value)
        sections[name] = value

    _add("node", lambda: node_summary())

    def _routes():
        from axiom.extensions.builtins.http.compose import (
            discovered_manifests,
            route_table,
        )

        # The REAL composition: manifest discovery included, exactly as
        # compose_app mounts a node (not just the built-in consumers).
        return [
            {
                "prefix": r.prefix,
                "extension": r.extension,
                "requires_authz": r.requires_authz,
            }
            for r in route_table(profile="server", manifests=discovered_manifests())
        ]

    _add("routes", _routes)

    def _chat_tools():
        from axiom.extensions.builtins.chat.tools import get_all_tools

        return sorted(get_all_tools())

    _add("chat_tools", _chat_tools)

    def _skills():
        from axiom.infra.skills import default_registry

        return sorted(default_registry().list())

    _add("skills", _skills)

    def _gate():
        from axiom.extensions.builtins.webgate.oidc import providers_from_env

        providers = providers_from_env()
        return {
            "oidc_providers": [{"name": p.name, "label": p.label} for p in providers],
            "self_signup": os.environ.get("AXIOM_GATE_SELF_SIGNUP", "").lower()
            in {"1", "true", "yes"},
        }

    _add("gate", _gate)

    def _rag():
        # The one section allowed to touch the DB — bounded and explicit.
        from axiom.extensions.builtins.settings.store import SettingsStore
        from axiom.rag.store import RAGStore

        url = SettingsStore().get("rag.database_url", "")
        if not url:
            return {"configured": False, "hint": "rag.database_url is not set"}
        return {"configured": True, **RAGStore(url, ensure_schema=False).stats()}

    _add("rag", _rag)

    def _db():
        from axiom.infra.db import platform_db_url

        url = platform_db_url()
        # Never leak credentials into an answer.
        safe = url.split("@")[-1] if "@" in url else url
        return {"target": safe}

    _add("db", _db)

    return {"sections": sections}


# ------------------------------------------------------------- prompt layer
def self_prompt_fragment() -> dict[str, Any]:
    """The capabilities-layer fragment: the voice rule + the fresh facts.
    Composed per turn (node_summary is cached, so this is ~free)."""
    s = node_summary()
    fed = s["federation"]
    if fed["configured"]:
        names = ", ".join(p["name"] for p in fed["peers"])
        fed_line = (
            f"I am federated with {len(fed['peers'])} peer(s): {names}. "
            "If asked about a site or peer I do not list here, I say plainly "
            "that I am not federated with it."
        )
    else:
        fed_line = "I am not federated with any peer or site right now."
    site_line = f"My site scope is '{s['site']}'." if s["site"] else "No site scope is set."
    content = (
        "SELF-KNOWLEDGE (answer in the FIRST PERSON — you ARE this node's "
        "agent; these facts are current, recomposed every turn):\n"
        f"- I am an Axiom node, version {s['version']['distribution']} "
        f"(running from {s['version']['source_path']}). {site_line}\n"
        f"- {fed_line}\n"
        f"- I have {len(s['extensions'])} extensions installed "
        f"({', '.join(s['extensions'][:12])}…).\n"
        "- For any deeper question about my own configuration — mounts, "
        "tools, skills, sign-in, RAG corpus, database — call the "
        "node_describe tool and answer from its result. NEVER speculate "
        "about my own setup, never say 'if such a function existed', and "
        "never refer to myself as 'the system'."
    )
    return {
        "layer": "capabilities",
        "name": "node_self_knowledge",
        "content": content,
        "source": "status",
        "required": True,
    }


__all__ = [
    "node_profile",
    "node_summary",
    "reset_summary_cache",
    "self_prompt_fragment",
]
