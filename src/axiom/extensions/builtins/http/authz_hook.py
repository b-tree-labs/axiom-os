# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The uniform AuthzHook adapter (RATIONALIZE-3).

The composed app (``compose_app``) already has the fail-closed contract:
``MountSpec.requires_authz=True`` is the default, and a mount that requires
authz is *refused* unless a ``MiddlewareConfig.authz`` hook is wired. What was
missing was the hook itself — nothing turned that seam into a real decision.

This module is that adapter. It bridges the HTTP request to the governance
``decide`` engine (``axiom.extensions.builtins.authz.decide``) so that EVERY
integration mounted on the composed app — LLM/gateway, RAG, ingestion push,
MCP-over-HTTP, callbacks — is gated uniformly with zero per-integration code.
Each just declares ``requires_authz=True``.

The core (:func:`build_authz_hook`) is pure: the principal resolver, the
decide function, and the envelope builder are injected. :func:`maybe_default_authz_hook`
wires the real engine and a token registry from the environment, and returns
``None`` when there is nothing safe to wire (so ``compose_app`` stays
fail-closed rather than serving auth-required routes unprotected).

The hook NEVER raises (the middleware calls it inline): every failure is a
fail-closed ``AuthzDecision(allow=False, ...)``.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from axiom.governance import (
    ActionEnvelope,
    ActionIntent,
    CapabilityToken,
    Classification,
    IntentPattern,
    NextAction,
    ProvenanceRef,
    ResourcePattern,
    ResourceRef,
    Verdict,
)
from axiom.vega.identity.principal import Principal
from axiom.webauth.api_keys import TOKEN_PREFIX

from .middleware import AuthzDecision, AuthzHook

_LOGGER = logging.getLogger("axi.serve")

# HTTP method → governance verb. The mount identity lives in the *resource*
# (so a single ``http.*`` rule can cover every mount, and production rules
# scope by ``resource_pattern``), not in the intent primitive.
_METHOD_VERB = {
    "GET": "read",
    "HEAD": "read",
    "OPTIONS": "read",
    "POST": "invoke",
    "PUT": "invoke",
    "PATCH": "invoke",
    "DELETE": "invoke",
}

@dataclass(frozen=True)
class ResolvedCredential:
    """A resolved bearer credential: the principal plus its granted scopes.

    Returned by the resolver for *issued* API keys (``gate.issue``). Empty
    scopes never occur on the issued-key path (issuance requires ≥1 scope);
    legacy env-registry tokens resolve to a bare :class:`Principal` instead,
    which the hook treats as unscoped — exactly the pre-#607 behavior.
    """

    principal: Principal
    scopes: tuple[str, ...] = ()
    credential_id: str | None = None


PrincipalResolver = Callable[[object], "Principal | ResolvedCredential | None"]
DecideFn = Callable[[ActionEnvelope], Verdict]
EnvelopeBuilder = Callable[[object, Principal], ActionEnvelope]


# ---------------------------------------------------------------------------
# Scope grammar — `<mount>[:<verb>]`
# ---------------------------------------------------------------------------

#: Scope verbs mirror the governance verbs HTTP methods map onto (plus "*").
_SCOPE_VERBS = frozenset({"read", "invoke", "access", "*"})
_SCOPE_MOUNT_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")


def parse_scope(scope: str) -> tuple[IntentPattern, ResourcePattern]:
    """Parse ``<mount>[:<verb>]`` into capability patterns.

    ``mount`` is the owning extension of a composed-app mount (``llm``,
    ``rag``, …) or ``*`` for all mounts; ``verb`` is one of ``read`` /
    ``invoke`` / ``access`` / ``*`` (default ``*``). Raises ``ValueError``
    on anything else — malformed scopes must never widen access.
    """
    raw = (scope or "").strip()
    mount, sep, verb = raw.partition(":")
    mount = mount.strip()
    verb = verb.strip() if sep else "*"
    if not mount or (mount != "*" and not _SCOPE_MOUNT_RE.match(mount)):
        raise ValueError(f"invalid scope mount: {scope!r}")
    if verb not in _SCOPE_VERBS:
        raise ValueError(
            f"invalid scope verb {verb!r} in {scope!r} "
            f"(expected one of {sorted(_SCOPE_VERBS)})")
    intent = IntentPattern("http.*" if verb == "*" else f"http.{verb}")
    resource = ResourcePattern(
        "extension://*" if mount == "*" else f"extension://{mount}/*")
    return intent, resource


def _covering_scope(
    scopes: tuple[str, ...],
    intent: ActionIntent,
    resource: ResourceRef,
) -> tuple[IntentPattern, ResourcePattern] | None:
    """The first scope covering (intent, resource), or ``None`` (deny).

    A malformed stored scope grants nothing (fail-closed) — it can only have
    entered the file by hand-editing, since issuance validates the grammar.
    """
    for scope in scopes:
        try:
            patterns = parse_scope(scope)
        except ValueError:
            continue
        if patterns[0].matches(intent) and patterns[1].matches(resource):
            return patterns
    return None


def _scoped_capability(
    subject: Principal,
    patterns: tuple[IntentPattern, ResourcePattern],
    credential_id: str | None,
) -> CapabilityToken:
    """A capability narrowed to the granted scope for this one decision.

    GUARD's capability floor (decide() step 1b) then sees least privilege —
    and every receipt records the scoped capability id instead of a wildcard.
    """
    now = datetime.now(timezone.utc)
    return CapabilityToken(
        id=f"apikey-{credential_id or uuid.uuid4().hex}",
        issuer=_principal_from_handle("@gate:apikeys"),
        subject=subject,
        intent_pattern=patterns[0],
        resource_pattern=patterns[1],
        classification_ceiling=Classification.INTERNAL,
        not_before=now - timedelta(seconds=1),
        not_after=now + timedelta(minutes=5),
        delegation_depth=0,
        parent_capability=None,
        signature=b"\x00" * 64,
    )


def default_envelope_builder(request: object, principal: Principal) -> ActionEnvelope:
    """Build an :class:`ActionEnvelope` describing this HTTP request.

    Intent is ``http.<verb>`` (verb from the method); the owning mount and
    path are carried in the resource URI ``extension://<mount><path>`` so a
    single ``http.*`` rule covers all mounts while production rules can still
    scope by resource.
    """
    state = getattr(request, "state", None)
    mount = getattr(state, "mount_extension", None) or "http"
    method = (getattr(request, "method", "") or "").upper()
    verb = _METHOD_VERB.get(method, "access")
    path = getattr(getattr(request, "url", None), "path", "") or "/"
    return ActionEnvelope(
        actor=principal,
        capability=CapabilityToken.unscoped_test_token(subject=principal),
        classification=Classification.INTERNAL,
        context_fragment_id=f"extension://{mount}",
        provenance_parent=ProvenanceRef.synthetic(f"http:{path}"),
        federation_origin=None,
        intent=ActionIntent(f"http.{verb}"),
        resource=ResourceRef.parse(f"extension://{mount}{path}"),
        deadline=None,
        dedup_key=f"http-{uuid.uuid4().hex}",
    )


def build_authz_hook(
    *,
    resolve_principal: PrincipalResolver,
    decide_fn: DecideFn,
    build_envelope: EnvelopeBuilder = default_envelope_builder,
) -> AuthzHook:
    """Build an :data:`AuthzHook` from injected seams. Pure + testable.

    Flow per request: resolve principal → (fail-closed if none) → build
    envelope → enforce credential scopes (issued API keys) → ``decide`` →
    allow iff the verdict says PROCEED. Any exception is swallowed into a
    fail-closed deny so the middleware never sees a raise.

    Scope enforcement is deterministic and engine-independent: when the
    resolved credential carries scopes and none covers the request's
    (intent, resource), the hook denies *before* consulting ``decide`` — so
    a permissive or absent engine (dev mode) can never widen an issued key
    beyond its grant. When a scope does cover the request, the envelope's
    capability is narrowed to that scope so GUARD's capability floor and the
    receipt trail see least privilege rather than a wildcard.
    """

    def hook(request: object) -> AuthzDecision:
        try:
            resolved = resolve_principal(request)
        except Exception as exc:  # noqa: BLE001 — resolution failure = deny
            _LOGGER.warning("authz principal resolution failed: %s", exc)
            return AuthzDecision(allow=False, reason="credential resolution failed")
        if resolved is None:
            return AuthzDecision(allow=False, reason="no valid credential")
        if isinstance(resolved, ResolvedCredential):
            principal, scopes = resolved.principal, resolved.scopes
            credential_id = resolved.credential_id
        else:
            principal, scopes, credential_id = resolved, (), None
        try:
            envelope = build_envelope(request, principal)
            if scopes:
                patterns = _covering_scope(scopes, envelope.intent,
                                           envelope.resource)
                if patterns is None:
                    return AuthzDecision(
                        allow=False,
                        reason="credential scopes do not cover this resource")
                envelope = replace(
                    envelope,
                    capability=_scoped_capability(principal, patterns,
                                                  credential_id))
            verdict = decide_fn(envelope)
        except Exception as exc:  # noqa: BLE001 — engine failure = fail-closed
            _LOGGER.warning("authz decide failed: %s", exc)
            return AuthzDecision(allow=False, reason=f"authz error: {exc}")
        # Attach the resolved principal for downstream handlers.
        state = getattr(request, "state", None)
        if state is not None:
            state.principal = principal
        if verdict.next_action_for_caller is NextAction.PROCEED:
            return AuthzDecision(allow=True)
        return AuthzDecision(allow=False, reason=verdict.reason)

    return hook


def _bearer_token(request: object) -> str | None:
    headers = getattr(request, "headers", {}) or {}
    # Starlette headers are case-insensitive; a plain dict in tests is not, so
    # try both spellings.
    raw = headers.get("authorization") or headers.get("Authorization")
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _principal_from_handle(handle: str) -> Principal:
    import hashlib

    if not handle.startswith("@"):
        handle = f"@{handle}"
    return Principal(handle=handle,
                     public_bytes=hashlib.sha256(handle.encode("utf-8")).digest())


def build_bearer_resolver(
    tokens: dict[str, str],
    *,
    api_keys: object | None = None,
    anonymous_handle: str | None = None,
) -> PrincipalResolver:
    """Resolve a ``Bearer`` token to a principal (or scoped credential).

    Two credential families, routed by shape:

    * **Issued API keys** (``axk_…``, minted by ``gate.issue``): resolved
      through ``api_keys`` — anything with a ``resolve(token) →
      ApiKeyIdentity | None`` shape, typically
      :class:`axiom.webauth.api_keys.JsonFileApiKeyStore`. The prefix routes
      *exclusively* here: an ``axk_`` token never falls back to the env
      registry, and resolves to a :class:`ResolvedCredential` carrying the
      key's scopes (hashed at rest; revocation immediate via hot-reload).
    * **Legacy env-registry tokens**: ``tokens`` maps secret → principal
      handle, untouched migration behavior.

    An unknown token resolves to ``None`` (deny). When no credential is
    presented and ``anonymous_handle`` is set, that handle is returned — a
    *dev-only* convenience for loopback use; leave it ``None`` in production
    so anonymous requests deny.
    """

    def resolve(request: object) -> Principal | ResolvedCredential | None:
        token = _bearer_token(request)
        if token is None:
            if anonymous_handle:
                return _principal_from_handle(anonymous_handle)
            return None
        if token.startswith(TOKEN_PREFIX):
            if api_keys is None:
                return None
            identity = api_keys.resolve(token)
            if identity is None:
                return None
            return ResolvedCredential(
                principal=_principal_from_handle(identity.principal),
                scopes=tuple(identity.scopes),
                credential_id=identity.key_id,
            )
        handle = tokens.get(token)
        if handle is None:
            return None
        return _principal_from_handle(handle)

    return resolve


def _load_token_registry() -> dict[str, str]:
    """Build the token→handle map from the environment.

    ``AXIOM_HTTP_API_KEYS`` is a comma-separated list of ``token:@handle``
    pairs. The legacy single ``AXIOM_API_KEY`` is honored as a token bound to
    ``@api:local`` so the gateway keeps working through the migration.
    """
    registry: dict[str, str] = {}
    raw = os.environ.get("AXIOM_HTTP_API_KEYS", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        token, _, handle = pair.partition(":")
        token, handle = token.strip(), handle.strip()
        if token and handle:
            registry[token] = handle
    legacy = os.environ.get("AXIOM_API_KEY", "").strip()
    if legacy:
        registry.setdefault(legacy, "@api:local")
    return registry


API_KEYS_FILE_ENV = "AXIOM_GATE_API_KEYS_FILE"
"""Where issued API keys live (the file ``gate.issue`` writes)."""


def _load_api_key_store():
    """The issued-keys store from the environment, or ``None``."""
    raw = os.environ.get(API_KEYS_FILE_ENV, "").strip()
    if not raw:
        return None
    from axiom.webauth.api_keys import JsonFileApiKeyStore

    return JsonFileApiKeyStore(raw)


def maybe_default_authz_hook() -> AuthzHook | None:
    """Wire the real authz engine + credential resolvers, or return ``None``.

    Returns ``None`` (so ``compose_app`` stays fail-closed and refuses
    auth-required mounts) when there is nothing safe to wire: production with
    no configured credentials (neither env-registry tokens nor an issued-keys
    file). In dev mode it wires a permit-all ``DecideContext`` (via
    ``setup_extension``) and an anonymous dev principal, so loopback
    development serves without per-request credentials.
    """
    from axiom.governance.mode import current_mode
    from axiom.governance.simple import get_current_actor, setup_extension

    dev = current_mode() == "dev"
    registry = _load_token_registry()
    api_keys = _load_api_key_store()

    if not dev and not registry and api_keys is None:
        _LOGGER.warning(
            "authz seam not wired: no AXIOM_HTTP_API_KEYS / AXIOM_API_KEY / "
            f"{API_KEYS_FILE_ENV} configured and not in dev mode — "
            "auth-required mounts will be refused (fail-closed). Configure "
            "credentials to enable the gateway.")
        return None

    ctx = setup_extension("http", verbs=["read", "invoke", "access"],
                          dev_mode=dev)
    authz_ctx = ctx.authz_ctx

    if authz_ctx is None:
        # No authz extension / DB. Only safe in dev (permit-all fallback).
        if not dev:
            return None
        decide_fn: DecideFn = lambda env: Verdict.from_decision(  # noqa: E731
            Decision.PERMIT, "dev mode: no authz engine wired",
            f"dev-{uuid.uuid4().hex}")
    else:
        from axiom.extensions.builtins.authz.decide import decide

        decide_fn = lambda env: decide(env, authz_ctx)  # noqa: E731

    anon = get_current_actor(dev_mode=True).handle if dev else None
    resolver = build_bearer_resolver(registry, api_keys=api_keys,
                                     anonymous_handle=anon)
    return build_authz_hook(resolve_principal=resolver, decide_fn=decide_fn)


# Imported late to avoid widening the module's import surface for the common
# (engine-wired) path.
from axiom.governance import Decision  # noqa: E402


__all__ = [
    "API_KEYS_FILE_ENV",
    "ResolvedCredential",
    "build_authz_hook",
    "build_bearer_resolver",
    "default_envelope_builder",
    "maybe_default_authz_hook",
    "parse_scope",
]
