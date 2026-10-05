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

import uuid
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


def authorize_site(
    requested: str,
    *,
    principal: str,
    bound: str = "",
    ctx: DecideContext | None = None,
    surface: str = "",
) -> str:
    """The site to query, or raise.

    ``requested`` is what the caller asked for and ``bound`` is what this
    install is bound to. The binding is a DEFAULT, not an authorisation: a
    fallback to it is decided on exactly like an explicit request, because
    "the node is bound to SENNA" is a statement about configuration and not
    about who is calling.

    Raises :class:`SiteNotAuthorized` when neither names a site. An unscoped
    read is not a read; widening it to every tenant is the one outcome that
    must never be reachable by omission.
    """
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
    "authorize_site",
    "build_site_context",
    "build_site_read_envelope",
    "decide_site_read",
    "default_site_context",
    "deny_site_rule",
    "open_site_rule",
    "propose_site_rule",
]
