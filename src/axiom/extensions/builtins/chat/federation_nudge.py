# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chat-time federation re-probe nudge.

Hooks into the chat-startup path (called from `render_welcome` /
chat CLI entry) and surfaces a one-line tip when a richer remote
LLM is reachable but the operator hasn't adopted it.

Closes the "chat finds a self-hosted node and suggests it" gap. Built on
top of `axiom.setup.federation_probe` (the install-time probe
primitive from PR #227) — chat reuses the discovery, adoption
check, and decline memo so the operator sees consistent state
across install-time and chat-time.

Two front doors share one probe (``_probe_for_nudge``):

  - ``maybe_render_federation_nudge()``: the startup call. Prints the tip
    itself; behaviour unchanged.
  - ``federation_advice(ctx)``: the first contributor of the post-command
    advisor hook (``advisor.py``). OFFERS the same tip mid-session, via
    the advisor's one-tip / decline-memo / cooldown discipline, and
    throttles the network probe so it does not re-run every turn.

Safety:
  - TTY-only (silent in CI / piped output)
  - Short timeout on probe — never blocks chat startup
  - Catches every exception — chat MUST start even if federation is
    unreachable / misconfigured / permission-denied
  - Respects the install-time decline memo so the prompt doesn't
    nag operators who already said no
  - Prints at most one tip even when many candidates are reachable
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axiom.setup.federation_probe import ProbeResult

    from .advisor import Advice, AdviceContext


def _is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty()) and bool(sys.stdin.isatty())
    except Exception:
        return False


def _llm_providers_path() -> Path:
    """Where the operator's adopted-providers file lives. Override
    via test-side monkeypatch."""
    from axiom.infra.paths import get_runtime_config_dir
    return get_runtime_config_dir() / "llm-providers.toml"


def _already_adopted(conn_name: str) -> bool:
    """True if `conn_name` is already present in llm-providers.toml.

    Defensive: missing file / parse error / no providers block → False
    (don't suppress the nudge on a file-read hiccup).
    """
    path = _llm_providers_path()
    if not path.exists():
        return False
    try:
        # tomllib stdlib in 3.11+; this codebase supports that floor.
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    gateway = data.get("gateway") or {}
    providers = gateway.get("providers") or []
    return any(p.get("name") == conn_name for p in providers)


def _has_declined(conn_name: str) -> bool:
    """Thin wrapper so tests can monkeypatch this module's seam
    rather than reaching into `axiom.setup.federation_probe`."""
    try:
        from axiom.setup.federation_probe import has_declined
        return bool(has_declined(conn_name))
    except Exception:
        return False


def _hostport(url: str) -> str:
    """The ``host:port`` of a URL, ignoring scheme and path — the grain the
    probe matches an LLM endpoint to its sibling RAG resource on."""
    return url.split("//", 1)[-1].split("/", 1)[0]


def _rag_adopted(rag_endpoint: str) -> bool:
    """True if ``rag.database_url`` already points at this endpoint's host:port.

    Defensive: a missing store / unset value / read error → False (a hiccup
    should not suppress the nudge). Compared at host:port grain because the
    served RAG face and the setting may differ by scheme or trailing path.
    """
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore
        current = SettingsStore().get("rag.database_url", "") or ""
    except Exception:
        return False
    return bool(current) and _hostport(current) == _hostport(rag_endpoint)


def _discover() -> list[ProbeResult]:
    """Discover reachable llm-category federation endpoints.

    Wrapped in its own seam so tests can stub it. Real impl creates
    a fresh ConnectionRegistry and probes; the probe itself uses
    `check_health` with a built-in timeout, so this returns quickly
    even when nothing is reachable.
    """
    from axiom.infra.connections import ConnectionRegistry
    from axiom.setup.federation_probe import discover_llm_endpoints

    registry = ConnectionRegistry()
    try:
        registry.discover_from_extensions()
    except Exception:
        return []
    return discover_llm_endpoints(registry)


def _cli_name() -> str:
    from axiom.infra.branding import get_branding
    return (get_branding().cli_name or "axi").strip()


@dataclass(frozen=True)
class _NudgePick:
    """What the probe settled on: an unadopted LLM (``kind == "llm"``, whose
    sibling RAG line may follow) or, when the LLM plane is fully adopted or
    declined, a bare unadopted RAG resource (``kind == "rag"``)."""

    kind: str
    result: Any
    cli: str


def _probe_for_nudge() -> _NudgePick | None:
    """Discover, then filter to the one thing worth nudging about, or None.

    Raises whatever ``_discover`` raises; both front doors catch it.
    """
    candidates = _discover()
    if not candidates:
        return None

    # Filter: not already adopted, not previously declined.
    usable: list = []
    for result in candidates:
        name = getattr(result.connection, "name", "")
        if not name:
            continue
        if _already_adopted(name):
            continue
        if _has_declined(name):
            continue
        usable.append(result)

    if usable:
        # Surface the first unadopted LLM. Multiple reachable providers
        # shouldn't spam the welcome banner — one is enough to prompt action.
        return _NudgePick(kind="llm", result=usable[0], cli=_cli_name())

    # LLM plane is fully adopted/declined — but the sibling RAG plane may
    # still be unadopted. Nudge it on its own if so.
    for result in candidates:
        cname = getattr(result.connection, "name", "")
        if cname and _has_declined(cname):
            continue
        if getattr(result, "rag_endpoint", None) and not _rag_adopted(result.rag_endpoint):
            return _NudgePick(kind="rag", result=result, cli=_cli_name())
    return None


def _llm_tip_text(pick: _NudgePick) -> str:
    name = pick.result.connection.name
    display = getattr(pick.result.connection, "display_name", name) or name
    return f"{display} is reachable. Adopt it with: {pick.cli} federation discover"


def _rag_tip_text(pick: _NudgePick) -> str | None:
    rag_endpoint = getattr(pick.result, "rag_endpoint", None)
    if not rag_endpoint or _rag_adopted(rag_endpoint):
        return None
    corpus = getattr(pick.result, "rag_corpus", None)
    label = f" ({corpus})" if corpus else ""
    return (
        f"its shared RAG corpus{label} and gold verbs are reachable too. "
        f"Ground chat on them with: {pick.cli} settings set rag.database_url {rag_endpoint}"
    )


def maybe_render_federation_nudge() -> None:
    """Pre-REPL nudge: one-line tip if a richer reachable LLM exists.

    No-op when:
      - stdin/stdout is not a TTY
      - probe finds no reachable candidates
      - the only reachable candidates are already adopted
        (in llm-providers.toml) OR were previously declined
        (federation_declined.json from PR #227)
      - any unhandled exception during probe (never block chat startup)
    """
    if not _is_tty():
        return

    try:
        pick = _probe_for_nudge()
    except Exception:
        return
    if pick is None:
        return

    if pick.kind == "llm":
        print(f"💡 Tip: {_llm_tip_text(pick)}")
    _maybe_render_rag_line(pick.result, pick.cli)


def _maybe_render_rag_line(pick, cli: str) -> None:
    """Second nudge line: the site's shared RAG (and the gold verbs that ride
    the same face) when the picked endpoint advertises a sibling RAG resource
    that ``rag.database_url`` is not yet pointed at.

    Kept separate so the domain plane (shared, on the site node) nudges
    independently of the LLM plane — the common case is a colleague who has
    the site's model but has not pointed chat at its corpus.
    """
    text = _rag_tip_text(_NudgePick(kind="rag", result=pick, cli=cli))
    if text is None:
        return
    print(f"   ↳ {text}")


# ---------------------------------------------------------------------------
# Advisor contributor: the same probe, OFFERED through the post-command hook
# ---------------------------------------------------------------------------

# Re-probe the network at most this often when the contributor is consulted
# after every turn. The advisor's own cooldown governs how often the tip is
# *shown*; this governs how often the probe *runs*.
ADVISOR_PROBE_INTERVAL_S = 300.0

_last_probe: tuple[float, _NudgePick | None] | None = None


def _reset_advisor_cache_for_tests() -> None:
    global _last_probe
    _last_probe = None


def _cached_pick() -> _NudgePick | None:
    global _last_probe
    now = time.monotonic()
    if _last_probe is not None and (now - _last_probe[0]) < ADVISOR_PROBE_INTERVAL_S:
        return _last_probe[1]
    try:
        pick = _probe_for_nudge()
    except Exception:
        pick = None
    _last_probe = (now, pick)
    return pick


def federation_advice(ctx: AdviceContext) -> Advice | None:
    """Advisor contributor: offer the adopt tip (or the RAG tip) as ``Advice``.

    Pure offer: the operator runs the printed command themselves. Keys are
    ``federation:<name>`` / ``federation-rag:<name>`` so the advisor's decline
    memo and cooldown apply per endpoint; the install-time decline memo is
    honoured upstream in ``_probe_for_nudge``.
    """
    from .advisor import Advice

    pick = _cached_pick()
    if pick is None:
        return None
    name = getattr(pick.result.connection, "name", "") or "endpoint"
    if pick.kind == "llm":
        return Advice(text=_llm_tip_text(pick), source="federation", key=f"federation:{name}")
    text = _rag_tip_text(pick)
    if text is None:
        return None
    return Advice(text=text, source="federation", key=f"federation-rag:{name}")
