# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Easy onramp for extension authors — the 5-line authz pattern.

The shipped GUARD + KEEP primitives have a low API surface but a high
*ergonomic* cost: an envelope construction site needs a Principal, a
CapabilityToken, a Classification, a ProvenanceRef, a ResourceRef, a
registered ActionIntent, and a dedup_key. Most extension authors don't
want to think about all that.

This module collapses the common case to:

    from axiom.governance.simple import setup_extension

    EXPMAN = setup_extension("expman", verbs=["transition_state",
                                              "create_sample"],
                             dev_mode=True)

    def transition(sample_id: str, to_state: str, actor: str):
        with EXPMAN.action(
            verb="transition_state",
            actor=actor,
            resource=f"extension://expman/sample/{sample_id}",
        ) as act:
            # GUARD has already approved or this block doesn't run.
            # `act.receipt_id` is the audit fragment id.
            ...do the transition...

That's it. No envelope construction, no rule wiring, no manual capability
issuance, no receipt plumbing. Dev mode permits everything with a receipt
trail; production mode swaps in real rules.

Resolution order for the **actor**:

1. ``axiom.governance.simple.set_current_actor(principal)`` if a CLI
   wrapper has set one for this process.
2. The ``AXIOM_ACTOR`` env var (e.g. ``@user:example-org``).
3. The ``actor`` argument to ``action()`` if a literal string handle.
4. A deterministic per-process fallback ``@dev:<hostname>`` in dev mode.
5. ``AuthnUnavailable`` otherwise.
"""

from __future__ import annotations

import hashlib
import os
import socket
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from axiom.governance.capability import CapabilityToken
from axiom.governance.classification import Classification
from axiom.governance.envelope import ActionEnvelope
from axiom.governance.intent import (
    ActionIntent,
    IntentPattern,
    register_intent,
)
from axiom.governance.provenance import ProvenanceRef
from axiom.governance.resource import ResourcePattern, ResourceRef
from axiom.governance.verdict import NextAction, Verdict
from axiom.vega.identity.principal import Principal


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuthnUnavailable(RuntimeError):
    """Raised when no actor can be resolved for an action."""


class AuthorizationDenied(PermissionError):
    """Raised inside ``action()`` when GUARD denies / asks for proposal.

    The verdict is attached so callers can inspect.
    """

    def __init__(self, verdict: Verdict, message: str = "") -> None:
        super().__init__(message or f"denied: {verdict.reason}")
        self.verdict = verdict


# ---------------------------------------------------------------------------
# Actor resolution
# ---------------------------------------------------------------------------

_actor_local = threading.local()


def set_current_actor(principal: Principal) -> None:
    """Bind a Principal as the current actor for this process / thread.

    A CLI wrapper typically calls this once at startup so subsequent
    ``action()`` calls don't need to be passed an actor.
    """
    _actor_local.principal = principal


def get_current_actor(*, dev_mode: bool = False) -> Principal:
    """Resolve the current actor per the documented order."""
    p = getattr(_actor_local, "principal", None)
    if isinstance(p, Principal):
        return p

    handle = os.environ.get("AXIOM_ACTOR")
    if handle:
        return _principal_from_handle(handle)

    if dev_mode:
        host = socket.gethostname().split(".")[0] or "localhost"
        return _principal_from_handle(f"@dev:{host}")

    raise AuthnUnavailable(
        "no actor resolvable; set AXIOM_ACTOR or call set_current_actor()"
    )


def _principal_from_handle(handle: str) -> Principal:
    """Build a Principal from a handle, deriving placeholder public bytes.

    Real production callers will use ``axiom.vega.identity`` to look up
    or mint a keypair-backed Principal. For dev mode, deterministic
    SHA-256 over the handle gives a stable placeholder.
    """
    if not handle.startswith("@"):
        handle = f"@{handle}"
    placeholder = hashlib.sha256(handle.encode("utf-8")).digest()
    return Principal(handle=handle, public_bytes=placeholder)


# ---------------------------------------------------------------------------
# Per-extension setup
# ---------------------------------------------------------------------------


@dataclass
class ExtensionAuthnContext:
    """The handle an extension uses to call into GUARD + KEEP.

    Constructed once by ``setup_extension`` and stored on the extension
    module. Every call site uses the ``action()`` context manager.
    """

    extension_name: str
    dev_mode: bool = False
    authz_ctx: object = None
    """Late-bound: a DecideContext from axiom.extensions.builtins.authz."""
    vault_ctx: object = None
    """Late-bound: a VaultContext from axiom.extensions.builtins.vault."""
    default_capability: CapabilityToken | None = None
    """A long-lived dev-mode capability; production swaps in real ones."""
    registered_verbs: list[str] = field(default_factory=list)

    @contextmanager
    def action(
        self,
        *,
        verb: str,
        actor: str | Principal | None = None,
        resource: str,
        classification: Classification = Classification.INTERNAL,
        dedup_key: str | None = None,
        capability: CapabilityToken | None = None,
    ) -> Iterator["_Action"]:
        """Build an envelope, consult GUARD, yield the action handle.

        Raises ``AuthorizationDenied`` on deny / propose; raises
        ``AuthnUnavailable`` if no actor resolvable.
        """
        principal = _resolve_actor_arg(actor, dev_mode=self.dev_mode)
        cap = capability or self.default_capability
        if cap is None:
            raise RuntimeError(
                f"extension {self.extension_name!r} has no default "
                "capability; pass one explicitly or use dev_mode=True"
            )
        intent_value = (
            verb if "." in verb else f"{self.extension_name}.{verb}"
        )
        env = ActionEnvelope(
            actor=principal,
            capability=cap,
            classification=classification,
            context_fragment_id=f"extension://{self.extension_name}",
            provenance_parent=ProvenanceRef.synthetic(
                f"{self.extension_name}:{verb}"
            ),
            federation_origin=None,
            intent=ActionIntent(intent_value),
            resource=ResourceRef.parse(resource),
            deadline=None,
            dedup_key=dedup_key or f"{self.extension_name}-{uuid.uuid4().hex}",
        )

        verdict = self._decide(env)
        if verdict.next_action_for_caller is not NextAction.PROCEED:
            raise AuthorizationDenied(verdict)

        yield _Action(envelope=env, verdict=verdict, ctx=self)

    def _decide(self, envelope: ActionEnvelope) -> Verdict:
        """Consult GUARD if wired; in dev mode without GUARD, permit."""
        if self.authz_ctx is not None:
            from axiom.extensions.builtins.authz.decide import decide

            return decide(envelope, self.authz_ctx)
        if self.dev_mode:
            return Verdict.from_decision(
                _DEV_PERMIT_DECISION,
                reason="dev mode: no DecideContext wired",
                receipt_fragment_id=f"dev-{uuid.uuid4().hex}",
            )
        raise RuntimeError(
            f"extension {self.extension_name!r} has no authz_ctx and "
            "is not in dev_mode; wire DecideContext or set dev_mode=True"
        )


@dataclass(frozen=True)
class _Action:
    """The yielded handle inside ``with EXT.action(...) as act:``."""

    envelope: ActionEnvelope
    verdict: Verdict
    ctx: ExtensionAuthnContext

    @property
    def receipt_id(self) -> str:
        return self.verdict.receipt_fragment_id


def _resolve_actor_arg(
    actor: str | Principal | None, *, dev_mode: bool
) -> Principal:
    if isinstance(actor, Principal):
        return actor
    if isinstance(actor, str):
        return _principal_from_handle(actor)
    return get_current_actor(dev_mode=dev_mode)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


class DevModeInProductionError(RuntimeError):
    """Raised when ``setup_extension(dev_mode=True)`` is called in a
    runtime that ``axiom.governance.mode.current_mode()`` reports as
    ``'production'``. This is a fail-closed safety check: the dev-mode
    permit-all rule must not silently leak into a production deploy.

    Operators who genuinely need dev-mode behaviour in production
    (e.g. for an integration test against a prod-shaped deploy) must
    set ``AXIOM_ALLOW_DEV_MODE_IN_PRODUCTION=1`` explicitly.
    """


def setup_extension(
    extension_name: str,
    *,
    verbs: list[str] | None = None,
    dev_mode: bool | None = None,
    wire_authz: bool = True,
    wire_vault: bool = True,
) -> ExtensionAuthnContext:
    """One-liner setup for extension authors. Returns a usable context.

    Args:
        extension_name: the extension's name (matches axiom-extension.toml).
        verbs: action verbs the extension produces. Each becomes a
            registered intent ``<extension_name>.<verb>``. If None, the
            common defaults are registered.
        dev_mode: if True, GUARD's absence permits with a receipt; KEEP's
            default capability is wildcard-scope, time-limited.
        wire_authz: if True, attempt to wire a DecideContext with
            ``session_for(extension_name)``. Silent fallback if the
            authz extension isn't installed.
        wire_vault: if True, attempt to wire a VaultContext similarly.

    The returned context exposes ``action()`` as the only call site
    extension authors typically use.
    """
    # Mode-aware dev_mode default: when the caller passes ``dev_mode=None``,
    # take the runtime mode as the source of truth (``current_mode()=='dev'``
    # ⇒ ``dev_mode=True``). When the caller passes ``dev_mode=True`` in a
    # production runtime, fail-closed unless an explicit env override is set.
    from axiom.governance.mode import current_mode

    runtime_mode = current_mode()
    if dev_mode is None:
        dev_mode = runtime_mode == "dev"
    elif dev_mode is True and runtime_mode == "production":
        if not os.environ.get("AXIOM_ALLOW_DEV_MODE_IN_PRODUCTION"):
            raise DevModeInProductionError(
                f"setup_extension({extension_name!r}, dev_mode=True) was "
                f"called but AXIOM_MODE={runtime_mode!r}. Refusing — the "
                "dev-mode permit-all rule would silently leak into prod. "
                "Set AXIOM_ALLOW_DEV_MODE_IN_PRODUCTION=1 to override "
                "intentionally (and document why)."
            )
        import logging
        logging.getLogger("axiom.governance.simple").warning(
            "extension %r set up with dev_mode=True in mode=%r — override "
            "active via AXIOM_ALLOW_DEV_MODE_IN_PRODUCTION; audit this.",
            extension_name, runtime_mode,
        )
    elif dev_mode is True and runtime_mode == "staging":
        import logging
        logging.getLogger("axiom.governance.simple").warning(
            "extension %r set up with dev_mode=True in mode=%r — verify "
            "this is intentional before going to production.",
            extension_name, runtime_mode,
        )

    verbs = verbs or ["invoke", "transition_state"]
    for v in verbs:
        register_intent(f"{extension_name}.{v}")

    ctx = ExtensionAuthnContext(
        extension_name=extension_name, dev_mode=dev_mode
    )

    if wire_authz:
        try:
            from axiom.extensions.builtins.authz import (
                DecideContext,
                Rule,
            )
            from axiom.infra.db import session_for

            ctx.authz_ctx = DecideContext(
                session_factory=lambda: session_for("authz")
            )
            if dev_mode:
                # Dev-mode permit-all for this extension's verbs.
                # Production callers add their own rules via
                # ``ctx.authz_ctx.add_rule(...)`` and remove this rule.
                ctx.authz_ctx.add_rule(
                    Rule(
                        name=f"dev_permit_{extension_name}",
                        intent_pattern=IntentPattern(f"{extension_name}.*"),
                        actor_pattern="*",
                        resource_pattern=ResourcePattern("*"),
                        disposition="permit",
                    )
                )
        except Exception:
            # GUARD not installed or DB unreachable; dev_mode picks up.
            pass

    if wire_vault:
        try:
            from axiom.extensions.builtins.vault import (
                VaultContext,
                issue_capability,
            )
            from axiom.infra.db import session_for

            vctx = VaultContext(
                session_factory=lambda: session_for("vault")
            )
            ctx.vault_ctx = vctx

            if dev_mode:
                # Issue a wildcard-scope capability for this process.
                dev_principal = get_current_actor(dev_mode=True)
                ctx.default_capability = issue_capability(
                    vctx,
                    subject=dev_principal,
                    intent_pattern=IntentPattern("*"),
                    resource_pattern=ResourcePattern("*"),
                    classification_ceiling=Classification.CONTROLLED,
                )
        except Exception:
            pass

    if ctx.default_capability is None and dev_mode:
        # Even without KEEP wired, give the extension a usable token.
        dev_principal = get_current_actor(dev_mode=True)
        ctx.default_capability = CapabilityToken.unscoped_test_token(
            subject=dev_principal
        )

    return ctx


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


# Imported here so the lazy dev-mode path stays self-contained.
from axiom.governance.verdict import Decision as _Dec  # noqa: E402

_DEV_PERMIT_DECISION = _Dec.PERMIT


__all__ = [
    "AuthnUnavailable",
    "AuthorizationDenied",
    "ExtensionAuthnContext",
    "get_current_actor",
    "set_current_actor",
    "setup_extension",
]
