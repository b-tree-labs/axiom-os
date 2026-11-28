# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Factory for creating the appropriate RAG store backend.

Postgres is the runtime store (ADR-174). A ``sqlite://`` URL is refused unless a test has
opted in with :func:`allow_sqlite_for_tests`: the SQLite store remains only so tests can run
without a server, and a runtime that quietly accepted it would be the fallback this decision
removed.
"""

from __future__ import annotations

import os
from typing import NamedTuple

_sqlite_allowed = False

#: The retrieval-store setting the chat, the MCP primitive and ``axi rag`` share. The read path.
RAG_URL_SETTING = "rag.database_url"

#: The writers' store (#2100): ingest, reindex, relabel and schema upgrades. A node whose serving
#: role may only SELECT keeps that role in :data:`RAG_URL_SETTING` and the owner role here. Empty:
#: writers use the read path, as one role for both always did.
RAG_WRITE_URL_SETTING = "rag.write_database_url"

#: The NAME of a vault entry (ADR-218) whose secret is the writers' whole ``postgresql://`` URL, so
#: the owner role's password need not sit in plain settings. Set, it is read when a writer connects
#: and takes precedence over :data:`RAG_WRITE_URL_SETTING`; an entry that cannot be read fails closed.
RAG_WRITE_VAULT_SETTING = "rag.write_database_vault"

_VAULT_ORIGIN_PREFIX = f"{RAG_WRITE_VAULT_SETTING} setting"


class WriteUrlUnavailable(ValueError):
    """``rag.write_database_vault`` names an entry that gives no usable URL. ``str()`` is one
    sentence naming the setting, the entry and the fix, and never any part of the secret."""


class StoreUrl(NamedTuple):
    """Which retrieval store a surface opens, and why: ``origin`` says where the URL came from."""

    url: str
    origin: str
    site: str = ""  # the bound site, when the URL is the binding's face

    def __repr__(self) -> str:
        # A URL read from the vault is a secret: a repr in a log or a traceback must not carry it.
        url = "<from the vault>" if self.origin.startswith(_VAULT_ORIGIN_PREFIX) else self.url
        return f"StoreUrl(url={url!r}, origin={self.origin!r}, site={self.site!r})"

    __str__ = __repr__


#: ADR-207 rule 11: a bound install grounds on the site's corpora through the node. A local store
#: configured on it is used only when this is set, and is then labelled as this machine's.
LOCAL_DOCUMENTS_WHEN_BOUND = "rag.local_documents_when_bound"


def _same_origin(a: str, b: str) -> bool:
    from urllib.parse import urlparse

    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def resolve_store_url(*, honour_env: bool = True) -> StoreUrl:
    """The retrieval store URL: ``DATABASE_URL``, then the serving mode, then ``rag.database_url``.

    A laptop bound to a site node (``site bind``) keeps that node's documents where its data
    already comes from: retrieval follows the binding's face URL, and :func:`create_store` sends
    the binding's key to it. Whether the install is bound is the one serving mode
    (``axiom.infra.serving_mode``, ADR-207), so documents and data cannot come from two places.
    A ``rag.database_url`` on a bound install is used only with the explicit opt-in
    :data:`LOCAL_DOCUMENTS_WHEN_BOUND` (or when it already names the bound node). A
    ``DATABASE_URL`` in the environment makes this install the medallion, so it is never bound,
    and counts as configured even for a surface that does not read it (``honour_env=False``).

    Configuration only: no network, never raises.
    """
    env = os.environ.get("DATABASE_URL") or ""
    if env and honour_env:
        return StoreUrl(env, "DATABASE_URL env")
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        settings = SettingsStore()
        setting = settings.get(RAG_URL_SETTING, "") or ""
        opted_in = str(settings.get(LOCAL_DOCUMENTS_WHEN_BOUND, "") or "").strip().lower() in {
            "1", "true", "yes", "on"}
    except Exception:  # noqa: BLE001 — settings absent is unconfigured
        setting, opted_in = "", False
    if env:
        return StoreUrl(setting, f"{RAG_URL_SETTING} setting") if setting else StoreUrl("", "")
    try:
        from axiom.infra.serving_mode import resolve_mode

        mode = resolve_mode()
    except Exception:  # noqa: BLE001 — an unreadable binding is no binding
        mode = None
    if mode is not None and mode.bound:
        if setting and _same_origin(setting, mode.face_url):
            return StoreUrl(setting, f"site binding {mode.site}", mode.site)
        if setting and opted_in:
            return StoreUrl(
                setting,
                f"{RAG_URL_SETTING} setting (this machine's documents, opted in while bound "
                f"to {mode.site})",
            )
        return StoreUrl(mode.face_url, f"site binding {mode.site}", mode.site)
    if setting:
        return StoreUrl(setting, f"{RAG_URL_SETTING} setting")
    return StoreUrl("", "")


def _setting(name: str) -> str:
    """A settings value as a stripped string, or ``""``. Never raises."""
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        return str(SettingsStore().get(name, "") or "").strip()
    except Exception:  # noqa: BLE001 - settings absent is unconfigured
        return ""


def _url_from_vault(entry: str) -> str:
    """The URL stored in the vault under ``entry``, read now. Raises :class:`WriteUrlUnavailable`.

    The same read path as the gate's ``*_CLIENT_SECRET_VAULT`` (``webgate.oidc``). No message here
    quotes the stored value or the vault backend's own error text, either of which could carry it.
    """
    setting = f"{RAG_WRITE_VAULT_SETTING}={entry!r}"
    fix = (
        f"store the owner role's postgresql:// URL with `axi vault set {entry}` (it reads the value "
        f"from stdin), or clear {RAG_WRITE_VAULT_SETTING} to use {RAG_WRITE_URL_SETTING}"
    )
    try:
        from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
        from axiom.infra.paths import get_user_state_dir

        with ForeignCredentialStore(get_user_state_dir()).get(entry) as secret:
            value = secret.as_str().strip()
    except KeyError:
        raise WriteUrlUnavailable(f"{setting} names no entry in the vault: {fix}.") from None
    except Exception as exc:  # noqa: BLE001 - any vault failure fails closed for writers
        raise WriteUrlUnavailable(
            f"{setting}: the vault could not be read ({type(exc).__name__}); RAG writers will not "
            f"fall back to another store. Check `axi vault diagnose`, or {fix}."
        ) from None
    if not value:
        raise WriteUrlUnavailable(f"{setting} is empty in the vault: {fix}.")
    if not value.startswith(("postgresql://", "postgres://", "postgresql+")):
        raise WriteUrlUnavailable(
            f"{setting} does not hold a postgresql:// URL (the entry must be the whole URL, not "
            f"only a password): {fix}."
        )
    return value


def configured_write_url() -> StoreUrl:
    """The writers' store as the settings configure it, ignoring ``DATABASE_URL``; empty if neither is set.

    ``rag.write_database_vault`` (read from the vault now, never cached), then
    ``rag.write_database_url``. The one place either setting is read, so every RAG writer honours
    both the same way. A vault setting whose entry gives no usable URL raises
    :class:`WriteUrlUnavailable`: writers fail closed rather than reach another store. Readers never
    call this.
    """
    entry = _setting(RAG_WRITE_VAULT_SETTING)
    if entry:
        return StoreUrl(_url_from_vault(entry), f"{_VAULT_ORIGIN_PREFIX} (vault entry {entry!r})")
    plain = _setting(RAG_WRITE_URL_SETTING)
    if plain:
        return StoreUrl(plain, f"{RAG_WRITE_URL_SETTING} setting")
    return StoreUrl("", "")


def resolve_write_url() -> StoreUrl:
    """The store a RAG writer opens: ``DATABASE_URL``, then ``rag.write_database_vault``, then
    ``rag.write_database_url``, then the read path.

    ``DATABASE_URL`` stays the highest-precedence override for writers and readers alike, so a one-off
    ``DATABASE_URL=... axi rag ...`` still means what it always meant. Raises
    :class:`WriteUrlUnavailable` when the vault setting names an entry that gives no usable URL.
    """
    env = os.environ.get("DATABASE_URL") or ""
    if env:
        return StoreUrl(env, "DATABASE_URL env")
    configured = configured_write_url()
    if configured.url:
        return configured
    return resolve_store_url()


def _bound_key_for(url: str) -> str:
    """The binding's key for ``url`` (see ``read_target.bound_key_for``); empty on any failure."""
    try:
        from axiom.extensions.builtins.data_platform.read_target import bound_key_for

        return bound_key_for(url)
    except Exception:  # noqa: BLE001 — no vault, no binding: no key, never a crash
        return ""


def allow_sqlite_for_tests(allowed: bool = True) -> None:
    """Tests opt in to the SQLite store; production never calls this."""
    global _sqlite_allowed
    _sqlite_allowed = allowed


def create_store(database_url: str, *, ensure_schema: bool = True):
    """Create a RAG store from a store URL.

    ``ensure_schema=False`` builds a store that will NOT run migration DDL on
    connect. A **monitoring** caller needs this: schema-ensure runs
    ``CREATE TABLE``, which fails with ``InsufficientPrivilege`` against the
    read-only role monitoring should be using — and a health check has no
    business migrating the schema it is inspecting. Honoured by the postgres
    backend; the others do not run migration DDL on construction.

    Supported schemes:
      - ``postgresql://`` / ``postgres://`` — local pgvector store
      - ``sqlite:///``                      — tests only (see ``allow_sqlite_for_tests``)
      - ``http://`` / ``https://``          — remote peer's retrieval endpoint
        (a node whose corpus lives on a peer; see ``remote_store``)
    """
    if not (database_url or "").strip():
        # No store configured is not a bad scheme. Saying "Unsupported store
        # URL scheme: ://" sent people looking for a typo that was not there.
        raise ValueError(
            "no RAG store is configured: set AXIOM_RAG_DSN to postgresql://… or an http(s):// peer"
        )
    if database_url.startswith("postgresql+"):
        # The platform's own spelling names the driver (require_named_driver);
        # psycopg2 takes the plain libpq form, and the meaning is the same.
        database_url = "postgresql://" + database_url.split("://", 1)[1]
    if database_url.startswith(("postgresql://", "postgres://")):
        from .store import RAGStore

        return RAGStore(database_url, ensure_schema=ensure_schema)
    elif database_url.startswith("sqlite://"):
        if not _sqlite_allowed:
            raise ValueError(
                "sqlite:// is not a runtime RAG store: Postgres is the runtime database (ADR-174). "
                "Use postgresql://… (the local stack provides one), or an http(s):// peer."
            )
        from .sqlite_store import SQLiteRAGStore

        return SQLiteRAGStore(database_url)
    elif database_url.startswith(("http://", "https://")):
        from .remote_store import RemoteRetrievalStore

        # Two credentials, for two kinds of caller. A peer node signs with the federation secret
        # (X-Node-ID + X-Signature, from the environment: it is a secret, so it never lives in the
        # store URL or committed settings; absent, the store's dev-mode defaults). A person's
        # laptop bound to a site node holds a gate key instead, and sends it as a bearer to that
        # node only, resolved exactly as a bound data read resolves it.
        kwargs: dict[str, str] = {}
        signature = os.environ.get("AXIOM_RAG_REMOTE_SIGNATURE", "")
        node_id = os.environ.get("AXIOM_RAG_REMOTE_NODE_ID", "")
        if signature:
            kwargs["signature"] = signature
        if node_id:
            kwargs["node_id"] = node_id
        bearer = _bound_key_for(database_url)
        if bearer:
            kwargs["bearer"] = bearer
        return RemoteRetrievalStore(database_url, **kwargs)
    else:
        raise ValueError(
            f"Unsupported store URL scheme: {database_url.split('://')[0]}://. "
            "Use postgresql:// or http(s):// (remote peer)."
        )
