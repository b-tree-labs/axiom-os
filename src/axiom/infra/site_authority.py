# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Which site may this caller read? One decision site, three surfaces.

Every read path took the tenant from the REQUEST and never checked the
CALLER. The resolver was ``site or config.site``: a caller who named a site
got it, and a bound node therefore restricted nothing, it only defaulted.
The same hole was reachable three ways — an explicit argument, an HTTP
``?site=`` query parameter, and a model-filled MCP argument — which is one
missing check, not three bugs.

This is that check. It is built on the authorization decision point that
already exists (:mod:`axiom.infra.authority` does the same for tool calls),
rather than as a second mechanism beside it, so a site read gets the same
rule engine, the same precedence (``deny > propose > permit``) and the same
receipts as everything else a caller does.

What a caller supplies is an identity and a request. What comes back is the
site to query, or an exception. Nothing returns a site the policy did not
agree to.

Open by default, on purpose
---------------------------
:func:`open_site_rule` permits ``data.*`` on ``site://*``, mirroring
``authority.open_tool_rule``. Without it a novel action with no matching
rule becomes *propose to a human*, which would turn every telemetry query
into an approval prompt. So this lands behaviour-neutral: what changes today
is that the decision HAPPENS and is recorded, which is what makes the hole
closable by adding a rule rather than by patching every caller.

A site closes it with :func:`deny_site_rule` or its own manifest rules. Those
outrank the open rule automatically, so the site policy only ever has to
*add*.

``propose`` is a refusal here
-----------------------------
A write can wait for a human. A read in the middle of a query cannot, so a
``propose`` verdict is reported as :class:`SiteNotAuthorized` with a message
saying approval is required, rather than blocking a query on a prompt
nobody is waiting at.
"""

from __future__ import annotations

import os
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from axiom.governance.classification import Classification
from axiom.governance.envelope import ActionEnvelope
from axiom.governance.intent import ActionIntent, IntentPattern
from axiom.governance.provenance import ProvenanceRef
from axiom.governance.resource import ResourcePattern, ResourceRef

if TYPE_CHECKING:  # pragma: no cover
    from axiom.extensions.builtins.authz.decide import DecideContext
    from axiom.extensions.builtins.authz.rules import Rule

#: Reading a site's data is a ``data`` action, not a bespoke verb: a site
#: rule written for data actions should cover it without knowing this module
#: exists.
SITE_READ_INTENT = "data.read"

#: ``site://<name>`` — the tenant as a resource, so a rule can name one site.
SITE_RESOURCE_SCHEME = "site"

#: Named so a receipt says WHY a read was allowed.
OPEN_SITE_RULE_NAME = "open_site_read"

DENY_SITE_RULE_PREFIX = "deny_site_read"
PROPOSE_SITE_RULE_PREFIX = "propose_site_read"


class SiteNotAuthorized(PermissionError):
    """This caller may not read this site.

    A ``PermissionError`` rather than a bespoke base, so a surface that
    already maps permission failures onto 403 or a refusal message does the
    right thing without being taught about tenancy.
    """


def _principal_from_handle(handle: str):
    """The same placeholder-key Principal the tool path builds.

    Imported rather than reimplemented: two functions deriving a Principal
    from a handle would be two chances to derive a different one, and the
    receipts would disagree about who acted.
    """
    from axiom.infra.authority import _principal_from_handle as _from_handle

    if not (handle or "").strip():
        raise ValueError("a site read needs a caller; the principal is empty")
    return _from_handle(handle)


def build_site_read_envelope(
    site: str,
    principal: str,
    *,
    surface: str = "",
    classification: str = "",
    strict: bool = False,
) -> ActionEnvelope:
    """The ``ActionEnvelope`` for one site read.

    ``principal`` is the ``@name:context`` handle the surface already
    carries; a malformed handle raises ``ValueError``, because a read whose
    caller cannot be identified is not a read that can be authorised.
    """
    from axiom.governance.capability import CapabilityToken

    actor = _principal_from_handle(principal)
    cls = (
        Classification.from_str(classification)
        if classification
        else Classification.INTERNAL
    )
    return ActionEnvelope(
        actor=actor,
        capability=CapabilityToken.unscoped_test_token(subject=actor),
        classification=cls,
        context_fragment_id=f"site://{site}",
        provenance_parent=ProvenanceRef.synthetic(f"site-read:{site}"),
        federation_origin=None,
        intent=ActionIntent(SITE_READ_INTENT),
        resource=ResourceRef(scheme=SITE_RESOURCE_SCHEME, identifier=site),
        deadline=None,
        dedup_key=f"site-read-{uuid.uuid4().hex}",
        surface=(surface or "").strip(),
        strict=strict,
    )


def decide_site_read(envelope: ActionEnvelope, ctx: DecideContext | None = None):
    """Consult the decision point for one site read."""
    from axiom.extensions.builtins.authz.decide import decide

    return decide(envelope, ctx if ctx is not None else default_site_context())


def _site_rule(name: str, site: str, disposition: str, priority: int = 0) -> Rule:
    from axiom.extensions.builtins.authz.rules import Rule

    return Rule(
        name=name,
        intent_pattern=IntentPattern(f"{ActionIntent(SITE_READ_INTENT).primitive}.*"),
        actor_pattern="*",
        resource_pattern=ResourcePattern(f"{SITE_RESOURCE_SCHEME}://{site}"),
        disposition=disposition,
        priority=priority,
    )


def open_site_rule() -> Rule:
    """Permit ``data.*`` on ``site://*`` — the open posture, by name."""
    return _site_rule(OPEN_SITE_RULE_NAME, "*", "permit", priority=-1000)


def deny_site_rule(site: str) -> Rule:
    """Refuse reads of one site. Outranks the open rule by disposition."""
    return _site_rule(f"{DENY_SITE_RULE_PREFIX}:{site}", site, "deny")


def propose_site_rule(site: str) -> Rule:
    """Require approval for one site, which for a read means refuse."""
    return _site_rule(f"{PROPOSE_SITE_RULE_PREFIX}:{site}", site, "propose")


def build_site_context(*, session_factory: Any | None = None) -> DecideContext:
    """A fresh context in the open posture — the minimal building block."""
    from axiom.extensions.builtins.authz.decide import DecideContext

    ctx = DecideContext(session_factory=session_factory)
    ctx.add_rule(open_site_rule())
    return ctx


_DEFAULT_CTX: DecideContext | None = None


def default_site_context() -> DecideContext:
    """The process-wide context, built once.

    Site rules are layered on by the same loader the tool path uses, so an
    operator writes one policy rather than one per surface.
    """
    global _DEFAULT_CTX
    if _DEFAULT_CTX is None:
        from axiom.infra.authority import _apply_site_rules, _receipt_session_factory

        ctx = build_site_context(session_factory=_receipt_session_factory())
        _apply_site_rules(ctx)
        _DEFAULT_CTX = ctx
    return _DEFAULT_CTX


def _reset_default_site_context() -> None:
    """Test seam, mirroring ``authority._reset_default_tool_context``."""
    global _DEFAULT_CTX
    _DEFAULT_CTX = None


#: The site the current request's credential is bound to, set by the HTTP authz
#: middleware from the verified credential itself (never from anything the caller sends).
_CALLER_SITE: ContextVar[str | None] = ContextVar("axiom_caller_site", default=None)

#: Whether the current request's credential reads every site on this node: the
#: owner's grant (scope ``*``, ADR-151), set by the same middleware from the same
#: verified credential. A site on the credential does not narrow it.
_CALLER_EVERY_SITE: ContextVar[bool] = ContextVar("axiom_caller_every_site", default=False)

#: Whether the current request's credential is an issued key that carries no site
#: (and not the owner's grant). Such a key reads no site's data: see
#: :data:`UNSITED_KEY_REFUSAL`. A caller with no credential at all (a terminal, a
#: stdio session) is not this; it is unchanged.
_CALLER_UNSITED_KEY: ContextVar[bool] = ContextVar("axiom_caller_unsited_key", default=False)

_FROM_REQUEST = object()

#: What a key with no site is told when it asks for site data. It says what changed,
#: why, and the one step that fixes it, which is the administrator's and keeps the
#: same key working (``axi gate rescope`` changes the record, never the token).
UNSITED_KEY_REFUSAL = (
    "This key is not tied to a site, so it cannot read site data: each key now reads "
    "only the site it was issued for, which keeps one site's readings from another. "
    "Nothing changes on your side: ask this node's administrator to tie the key to "
    "your site (`axi gate rescope api-key <key-id> --site <site>`), and the same key "
    "keeps working."
)


def held_site_note(own: str) -> str:
    """The sentence every refusal of another site's data carries.

    Plain about what holds and why, and the same whichever site was asked for, so it
    never confirms that another site is hosted here.
    """
    return (
        f"Each key reads only the site it was issued for, and this one was issued "
        f"for {own!r}."
    )


def bind_caller_site(site: str | None, *, every_site: bool = False,
                     unsited_key: bool = False):
    """Record the site the current request's credential is bound to.

    ``every_site`` records that the credential holds the owner's grant, which
    reads every site whatever site it was minted at. ``unsited_key`` records an
    issued key with no site, which reads no site's data. Returns the token for
    :func:`unbind_caller_site`. Called by the HTTP authz middleware once a
    credential has been verified; nothing a caller supplies reaches it.
    """
    site = (site or "").strip() or None
    return (
        _CALLER_SITE.set(site),
        _CALLER_EVERY_SITE.set(bool(every_site)),
        _CALLER_UNSITED_KEY.set(bool(unsited_key) and site is None and not every_site),
    )


def unbind_caller_site(token) -> None:
    """Undo :func:`bind_caller_site` at the end of the request."""
    site_token, every_token, unsited_token = token
    _CALLER_UNSITED_KEY.reset(unsited_token)
    _CALLER_EVERY_SITE.reset(every_token)
    _CALLER_SITE.reset(site_token)


def caller_is_unsited_key() -> bool:
    """Whether the current request's credential is an issued key with no site."""
    return _CALLER_UNSITED_KEY.get()


def caller_site() -> str | None:
    """The site the current request's credential is bound to, if any."""
    return _CALLER_SITE.get()


def caller_reads_every_site() -> bool:
    """Whether the current request's credential holds the owner's every-site grant."""
    return _CALLER_EVERY_SITE.get()


def tenant_scope(*, bound: str = "") -> str | None:
    """The one site this request's credential is held to, or ``None``.

    ``None`` means the caller is not held to a site: an operator at the
    terminal, no credential at all (a stdio session), or a credential holding
    the owner's grant (scope ``*``). Every other credential bound to a site is
    held to that site, and what it may read is that site and nothing else.

    An issued key that carries no site reads nothing: this raises
    :class:`SiteNotAuthorized` with :data:`UNSITED_KEY_REFUSAL`. Such a key used to
    read every site. The serving app ties today's no-site keys to the node's home
    site at startup, in place (:func:`axiom.webauth.api_keys.assign_site_to_unsited_keys`),
    so this is the fallback for one that appears later, not a change any holder sees.

    The node's home site is NOT an exemption (ADR-203: "any credential bound to
    a site, the node's home site included, sees that site's objects only"). It
    used to be one, so a viewer key minted for the home site read every partner
    hosted beside it. ``bound`` is accepted for callers that pass it and no
    longer widens anything.

    :func:`authorize_site` answers this for a read that NAMES a site. A verb that
    takes a table and a filter names its site inside the filter text, or omits it,
    so it needs the scope itself, to apply as a predicate the caller cannot
    remove. Read from the verified credential, never from anything the caller sent.
    """
    del bound
    if _CALLER_UNSITED_KEY.get():
        raise SiteNotAuthorized(UNSITED_KEY_REFUSAL)
    own = _CALLER_SITE.get()
    if not own or _CALLER_EVERY_SITE.get():
        return None
    return _resolved(own)


def _resolved(site: str) -> str:
    from axiom.infra.site_identity import resolve as _resolve_site

    return _resolve_site(site.strip())


#: Further sites, beyond ``AXIOM_SITE``, that belong to the organisation operating
#: this node (comma-separated). This says whose *documents* a credential may read
#: (the operator organisation's corpus); it never widens a data read, which every
#: site-bound credential makes for its own site only.
HOME_SITES_ENV = "AXIOM_HOME_SITES"


def is_home_site(site: str | None) -> bool:
    """Whether ``site`` is one of the sites of the organisation operating this node.

    For choosing a credential's document corpora, not its data: a home-site key
    reads the operator organisation's own material, and its readings stay held to
    its own site by :func:`tenant_scope` like every other credential's.
    """
    if not (site or "").strip():
        return False
    names = [os.environ.get("AXIOM_SITE", "")] + os.environ.get(HOME_SITES_ENV, "").split(",")
    return _resolved(str(site)) in {_resolved(n) for n in names if n and n.strip()}


def _hold_to_tenant(requested: str, *, own: str, principal: str, every_site: bool) -> str:
    """A site-bound credential's read: its own site, or a refusal.

    The open posture lets any identified caller read any site, and a rule can
    only name a literal handle, so without this a credential reads its
    neighbours by naming them. Only the owner's grant reads across sites.
    """
    if every_site:
        return requested
    own_site = _resolved(own)
    asked = _resolved(requested) if (requested or "").strip() else own_site
    if asked != own_site:
        raise SiteNotAuthorized(
            f"{principal} may not read site {asked!r}. {held_site_note(own_site)}"
        )
    return own_site


def authorize_site(
    requested: str,
    *,
    principal: str,
    bound: str = "",
    ctx: DecideContext | None = None,
    surface: str = "",
    caller_site: Any = _FROM_REQUEST,
) -> str:
    """The site to query, or raise.

    ``requested`` is what the caller asked for and ``bound`` is what this
    install is bound to. The binding is a DEFAULT, not an authorisation: a
    fallback to it is decided on exactly like an explicit request, because
    "the node is bound to SENNA" is a statement about configuration and not
    about who is calling.

    ``caller_site`` is the site the caller's credential is bound to; by
    default it is read from the current request (the HTTP authz hook records
    it). A credential bound to a site reads that site only, the node's home
    site included, and a read naming no site means it. Only a credential
    holding the owner's grant (scope ``*``) reads across sites.

    Raises :class:`SiteNotAuthorized` when neither names a site. An unscoped
    read is not a read; widening it to every tenant is the one outcome that
    must never be reachable by omission.
    """
    if caller_site is _FROM_REQUEST and _CALLER_UNSITED_KEY.get():
        raise SiteNotAuthorized(UNSITED_KEY_REFUSAL)
    own = _CALLER_SITE.get() if caller_site is _FROM_REQUEST else caller_site
    if own:
        every = _CALLER_EVERY_SITE.get() if caller_site is _FROM_REQUEST else False
        requested = _hold_to_tenant(requested, own=own, principal=principal, every_site=every)
    site = (requested or "").strip() or (bound or "").strip()
    # Resolved before it is authorised, so a reader naming a site by an id
    # it used to have is authorised for — and reads — the one dataset. A
    # rename that forked the data was the failure this closes; a rename
    # that forked the AUTHORISATION would be the same failure wearing a
    # different hat.
    if site:
        from axiom.infra.site_identity import resolve as _resolve_site

        site = _resolve_site(site)
    if not site:
        raise SiteNotAuthorized(
            "no site named and this install is not bound to one, so there is "
            "nothing to scope this read to. Pass a site or run `neut site bind`."
        )

    envelope = build_site_read_envelope(site, principal, surface=surface)
    verdict = decide_site_read(envelope, ctx)

    disposition = getattr(verdict, "disposition", verdict)
    decision = str(getattr(disposition, "value", disposition)).lower()
    rule = getattr(verdict, "rule_name", "") or getattr(verdict, "matched_rule", "")

    if "permit" in decision:
        return site
    if "propose" in decision:
        raise SiteNotAuthorized(
            f"{principal} may read site {site!r} only with human approval "
            f"(rule {rule or 'unnamed'}), and a read cannot wait for one. "
            "Ask an operator to widen the policy, or query a site you hold."
        )
    raise SiteNotAuthorized(
        f"{principal} is not authorised to read site {site!r}"
        + (f" (rule {rule})" if rule else "")
    )


__all__ = [
    "DENY_SITE_RULE_PREFIX",
    "OPEN_SITE_RULE_NAME",
    "PROPOSE_SITE_RULE_PREFIX",
    "SITE_READ_INTENT",
    "SITE_RESOURCE_SCHEME",
    "SiteNotAuthorized",
    "UNSITED_KEY_REFUSAL",
    "authorize_site",
    "caller_is_unsited_key",
    "caller_reads_every_site",
    "held_site_note",
    "is_home_site",
    "tenant_scope",
    "build_site_context",
    "build_site_read_envelope",
    "decide_site_read",
    "default_site_context",
    "deny_site_rule",
    "open_site_rule",
    "propose_site_rule",
]
