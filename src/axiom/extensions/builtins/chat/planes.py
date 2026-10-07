# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Plane status: one answer to "what am I connected to?" (P2e).

The harness-agnostic site-access spec models a chat user's connectivity as
four planes, each with a single writer:

  domain   retrieval: the corpus and the retrieval endpoint (RAG)
  person   identity: the acting principal and how it was resolved
  session  memory: where this user's cross-session ledger lives
  trace    logs: where retrieval / interaction events are recorded

For each plane this module reports its *source* and whether it is ``local``
(resolved on this node), ``served`` (a configured endpoint answers) or
``unconfigured`` (nothing wired). Resolution reads configuration only:
settings, environment and files on disk. It never opens a network
connection, so ``/planes`` is safe on an air-gapped node and never hangs,
and it never raises: a resolver that fails degrades to ``unconfigured``
naming the exception class. Nothing here names a site; the report says
whatever this node's configuration resolves to.

Secrets are redacted: URL passwords, userinfo, paths and query strings never
reach the output, and identity tokens are never read at all.

Surfaces: ``/planes`` (slash command), the ``plane_status`` READ chat tool,
and one ``Retrieval:`` line in ``/status``. All three call :func:`plane_report`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

PlaneKind = Literal["local", "served", "unconfigured"]

#: Plane names in report order, per the spec's plane model.
PLANES: tuple[str, ...] = ("domain", "person", "session", "trace")

#: The retrieval-store setting the chat, the MCP primitive and ``axi rag`` share.
RAG_URL_SETTING = "rag.database_url"

KIND_LEGEND = (
    "local = resolved on this node; served = a configured endpoint answers; "
    "unconfigured = nothing wired"
)


@dataclass(frozen=True)
class PlaneStatus:
    """One plane's resolution: where it lives and what it resolves to."""

    name: str
    kind: str  # PlaneKind
    source: str  # URL / path / provider label, secrets redacted; "" when none
    detail: str = ""


# ---------------------------------------------------------------------------
# URL redaction
# ---------------------------------------------------------------------------


def _hostport(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    return f"{host}:{parts.port}" if parts.port else host


def _served_source(url: str) -> str:
    """``scheme://host[:port]`` only: no userinfo, no path, no query."""
    return f"{urlsplit(url).scheme}://{_hostport(url)}"


def _postgres_source(url: str) -> str:
    """``scheme://[user@]host[:port]/db`` with the password dropped."""
    parts = urlsplit(url)
    user = f"{parts.username}@" if parts.username else ""
    return f"{parts.scheme}://{user}{_hostport(url)}/{parts.path.lstrip('/')}"


def _sqlite_source(url: str) -> str:
    """The file path, parsed the way ``SQLiteRAGStore`` parses it."""
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///") :]
    return url[len("sqlite://") :]


def describe_store_url(url: str) -> tuple[str, str, str]:
    """``(kind, source, note)`` for a retrieval-store URL, secrets stripped.

    Mirrors ``axiom.rag.store_factory.create_store``'s scheme table without
    constructing a store: ``http(s)`` is a served retrieval endpoint,
    ``postgresql``/``sqlite`` are stores this node queries itself.
    """
    if not url:
        return "unconfigured", "", "no retrieval store"
    if url.startswith(("http://", "https://")):
        return "served", _served_source(url), "retrieval endpoint"
    if url.startswith(("postgresql://", "postgres://")):
        return "local", _postgres_source(url), "pgvector store"
    if url.startswith("sqlite://"):
        return "local", _sqlite_source(url), "sqlite store"
    scheme = url.split("://", 1)[0]
    return "unconfigured", "", f"unsupported store scheme {scheme}://"


# ---------------------------------------------------------------------------
# Resolvers (configuration only; no network)
# ---------------------------------------------------------------------------


def _retrieval_url() -> tuple[str, str]:
    """``(url, origin)``: ``DATABASE_URL`` env first, then the setting.

    The same order as the MCP ``rag.retrieve`` primitive and ``axi rag``,
    so this surface reports the store those would actually open.
    """
    url = os.environ.get("DATABASE_URL") or ""
    if url:
        return url, "DATABASE_URL env"
    from axiom.extensions.builtins.settings.store import SettingsStore

    url = SettingsStore().get(RAG_URL_SETTING, "") or ""
    return url, f"{RAG_URL_SETTING} setting"


def resolve_domain() -> PlaneStatus:
    """Domain plane: the retrieval store or endpoint this chat grounds on."""
    url, origin = _retrieval_url()
    kind, source, note = describe_store_url(url)
    if not url:
        detail = f"{note}: set {RAG_URL_SETTING} (or DATABASE_URL)"
    else:
        detail = f"{note} via {origin}"
    return PlaneStatus("domain", kind, source, detail)


_PROVIDER_BY_POSTURE = {
    "open": "os-session",
    "attested": "local-keypair",
    "service": "service",
}


def _idp_label() -> str:
    """The identity provider an ``sso`` posture resolves through, by config."""
    from axiom.extensions.builtins.settings.store import SettingsStore

    tenant = SettingsStore().get("user.org_tenant", "") or ""
    return "entra (tenant configured)" if tenant else "sso (no provider configured)"


def _ledger_principal() -> tuple[str | None, str]:
    """``(principal, note)`` from the node identity the memory wiring uses."""
    from .memory_wiring import _principal_id_from_identity

    try:
        principal = _principal_id_from_identity()
    except Exception as exc:  # noqa: BLE001 - a broken identity file is a note, not a failure
        log.debug("plane status: node identity unreadable: %s", exc)
        return None, f"node identity unreadable ({type(exc).__name__})"
    if principal:
        return principal, f"memory principal {principal}"
    return None, "no node identity"


def resolve_person() -> PlaneStatus:
    """Person plane: who is acting, at what posture, resolved by whom.

    Reads the node's posture floor and the OS handle; never touches the
    keychain or an IdP, so no token is read and nothing can prompt.
    """
    from axiom.infra.principal import local_handle, node_posture

    posture = node_posture()
    handle = local_handle()
    provider = _PROVIDER_BY_POSTURE.get(posture) or _idp_label()
    kind = "served" if posture in ("sso", "service") else "local"
    assurance = "unproven" if posture == "open" else "assured"
    _, note = _ledger_principal()
    detail = f"posture {posture} ({assurance}); {note}"
    return PlaneStatus("person", kind, f"{provider} {handle}", detail)


def resolve_session() -> PlaneStatus:
    """Session plane: where this user's sessions and ledger live.

    This used to report a hardcoded SQLite file — "always this user's own
    SQLite store under the state dir, never the shared node" — which is the
    one shape that cannot support a second surface, and which the standing
    rule reserves for local testing. It now reports the store that would
    actually answer.

    A database on ``localhost`` reports ``local``, not ``served``. It is the
    right backend and still one machine's database; calling it served would
    claim a shared store no other surface can reach.
    """
    principal, note = _ledger_principal()
    if not principal:
        return PlaneStatus("session", "unconfigured", "", f"{note}: chat runs stateless")

    from axiom.infra.orchestrator.session_backend import choose_session_backend

    choice = choose_session_backend()
    if choice.kind == "files":
        from axiom.infra.paths import get_user_state_dir

        ledger = get_user_state_dir() / "memory" / "artifacts.db"
        state = "present" if ledger.exists() else "created on first turn"
        return PlaneStatus(
            "session",
            "local",
            str(ledger),
            f"{note} ({state}); file store — local testing only",
        )
    return PlaneStatus("session", choice.plane_kind, choice.source, f"{note}; {choice.reason}")


def resolve_trace(domain: PlaneStatus | None = None) -> PlaneStatus:
    """Trace plane: where retrieval and interaction events are logged.

    Follows the domain plane: a served retrieval endpoint writes its own
    ``retrieval_log`` per call; a local store carries the ``retrieval_log``
    and ``interaction_log`` tables itself; no store, nothing to log.
    """
    domain = domain if domain is not None else resolve_domain()
    if domain.kind == "served":
        detail = "retrieval_log written by the retrieval endpoint on every call"
        return PlaneStatus("trace", "served", domain.source, detail)
    if domain.kind == "local":
        detail = "retrieval_log + interaction_log tables in the retrieval store"
        return PlaneStatus("trace", "local", domain.source, detail)
    return PlaneStatus("trace", "unconfigured", "", "follows the domain plane: nothing to log")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _safe(name: str, resolve: Callable[[], PlaneStatus]) -> PlaneStatus:
    """Run one resolver; a failure is a plane status, never an exception.

    Only the exception class is reported: messages may echo a URL or a
    credential, and this surface must stay secret-free.
    """
    try:
        return resolve()
    except Exception as exc:  # noqa: BLE001 - a status surface never raises
        log.debug("plane status: %s plane unresolved: %r", name, exc)
        return PlaneStatus(name, "unconfigured", "", f"unresolved: {type(exc).__name__}")


def plane_report() -> list[PlaneStatus]:
    """Resolve every plane, in :data:`PLANES` order, without network access."""
    domain = _safe("domain", resolve_domain)
    person = _safe("person", resolve_person)
    session = _safe("session", resolve_session)
    trace = _safe("trace", lambda: resolve_trace(domain))
    return [domain, person, session, trace]


def retrieval_summary() -> str:
    """One line for ``/status``: the domain plane's kind and source."""
    domain = _safe("domain", resolve_domain)
    if domain.source:
        return f"{domain.kind} plane ({domain.source})"
    return f"{domain.kind} (see /planes)"


def format_plane_report(planes: Iterable[PlaneStatus], *, width: int | None = None) -> str:
    """Render the plane table for ``/planes``: name, kind, source, detail.

    Goes through :func:`axiom.infra.cli_format.table` rather than padding by
    hand. The hand-rolled version sized itself to its own content and left the
    terminal to wrap the result, which tore a detail sentence in half and put
    the remainder at an indent belonging to no column — "no" on one line and
    "retrieval store:" on the next. A table that owns its width keeps each
    cell in its own lane however narrow the window gets.
    """
    from axiom.infra.cli_format import SQUARE, Column, table, terminal_width

    # 4 cells of chat chrome (scrollbar, gutter, wrap margin) plus the
    # 2-cell indent. A hardcoded width overflows the pane by exactly the
    # amount nobody notices until the right edge wraps.
    if width is None:
        width = terminal_width(reserve=6)

    rows = list(planes)
    lines = ["", "  Planes: domain=retrieval  person=identity  session=memory  trace=logs"]
    if not rows:
        lines.append("    (no planes resolved)")
    else:
        columns = [
            Column("plane"),
            Column("kind"),
            # Both may give up width: a store URL and a status sentence are
            # each occasionally long, and neither should starve the other.
            Column("source", wrap=True),
            Column("detail", wrap=True),
        ]
        data = [(p.name, p.kind, p.source or "-", p.detail or "") for p in rows]
        # No border here, deliberately, and verified rather than assumed:
        # driving the real TUI through a pty with a bordered table produced
        # collapsed padding, rules wrapping onto their own line, and an eaten
        # character ("memry principal"). prompt-toolkit's renderer treats the
        # vertical as a column separator. Aligned columns without a frame
        # survive it intact; that is what this surface gets.
        # Bordered: the pane passes pre-rendered box-drawing straight through
        # now that `_word_wrap` recognises it. Before that fix the TUI's own
        # markdown aligner re-laid the table out and collapsed the padding.
        lines.extend(
            table(data, columns, width=width, headers=True,
                  border=SQUARE)
        )
    lines.append(f"  {KIND_LEGEND}")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "KIND_LEGEND",
    "PLANES",
    "RAG_URL_SETTING",
    "PlaneKind",
    "PlaneStatus",
    "describe_store_url",
    "format_plane_report",
    "plane_report",
    "resolve_domain",
    "resolve_person",
    "resolve_session",
    "resolve_trace",
    "retrieval_summary",
]
