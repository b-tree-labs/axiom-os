# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.new`` and ``attest.sign``: a person signs at the CLI.

Both end the same way: the presentation is printed, the person answers
``sign``, ``hold`` or ``ask`` at the prompt, and only ``sign`` chains a record.
There is no flag that answers for them, and with no one at the prompt nothing
is signed. These skills are exposed on the CLI only, never over MCP or as an
agent tool, because software drafts and never signs (ADR-142).
"""

from __future__ import annotations

import time
from typing import Any

from axiom.infra.skills import SkillResult
from axiom.vega.identity.node_key import NodeKeyUnavailable

from .. import service, signing
from ..logbooks import LogbookError
from ..roles import roles_for
from ..service import AttestRefused, Signatory
from ._common import fail, parse_fields, resolve_site


def _beyond_the_prompt(draft_id: str, ctx: Any) -> str | None:
    """Why the command line cannot sign this draft, or None. The prompt carries no grant
    (ADR-146): no fresh sign-in, no presence proof, and at most the ``attested`` posture, so a
    draft that needs any of them is not put to the person here (#1669)."""
    from axiom.infra.principal import PrincipalContext

    from ..logbooks import SIGNING_POSTURES

    rules = service.rules_for(draft_id)
    cli_most = "attested"  # what the prompt can reach (AXIOM_IDENTITY_POSTURE=attested)
    held = PrincipalContext(handle=ctx.principal.handle, posture=ctx.principal.posture,
                            assured=True)  # fmt: skip
    beyond = SIGNING_POSTURES.index(rules.posture) > SIGNING_POSTURES.index(cli_most)
    if beyond and not held.meets(rules.posture):
        return (
            f"{rules.what} needs posture {rules.posture!r}; the command line reaches at most "
            f"{cli_most!r}"
        )
    if rules.fresh_within_seconds is not None:
        return f"{rules.what} needs a sign-in within {rules.fresh_within_seconds}s"
    if rules.presence:
        return f"{rules.what} must be signed at {rules.presence}"
    return None


def _respond(draft_id: str, site: str, ctx: Any) -> SkillResult:
    if ctx is None or ctx.user_prompt is None:
        return fail("signing needs a person at the prompt; nothing was signed")
    try:
        beyond = _beyond_the_prompt(draft_id, ctx)
    except (AttestRefused, LogbookError) as exc:
        return fail(f"not signed: {exc}")
    from .. import gate_handoff

    handoff = gate_handoff.current() if beyond is not None else None
    if beyond is not None and handoff is None:
        return fail(
            f"not signed: {beyond}, which the command line cannot give, and this node names no "
            f"web address for its gate. The draft {draft_id} waits for you: sign it in the web "
            "app (Waiting for you), where a fresh sign-in and the device can be proved."
        )
    handle = ctx.principal.handle
    who = Signatory(
        principal=ctx.principal,
        roles=roles_for(handle, site, state_dir=ctx.state_dir),
        display=handle,
    )
    try:
        pres = service.present(draft_id, modality="cli")
        started = time.monotonic()
        answer = (
            ctx.user_prompt(f"{pres.rendered}\n\ndigest {pres.digest}\nsign, hold or ask? ")
            .strip()
            .lower()
        )
        service.respond(
            pres.presentation_id,
            principal=ctx.principal,
            answer=answer,
            via="cli",
            transcript=answer,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if answer != "sign":
            return SkillResult(ok=True, value={"status": answer, "draft_id": draft_id})
        grant = None
        if handoff is not None:
            # The prompt cannot prove the sign-in this entry needs; the node's gate can (#1669).
            grant, why = gate_handoff.obtain_grant(pres.presentation_id, handoff)
            if grant is None:
                return fail(f"not signed: {why}. The draft {draft_id} waits for you; run sign "
                            "again or sign it in the web app.")  # fmt: skip
        result = service.sign(
            pres.presentation_id, signatory=who, signer=signing.signer(), source="cli",
            grant=grant, grant_keys=signing.public_keys() if grant else None,
        )  # fmt: skip
    except (AttestRefused, NodeKeyUnavailable) as exc:
        return fail(f"not signed: {exc}")
    except Exception as exc:  # noqa: BLE001 - a grant refused at signing is a refusal, not a crash
        from ..grants import GrantRefused

        if isinstance(exc, GrantRefused):
            return fail(f"not signed: {exc}. The draft {draft_id} waits for you.")
        raise
    rec = result.record
    value = {
        "status": "signed",
        "attestation_id": rec["attestation_id"],
        "site_id": rec["site_id"],
        "logbook": rec["logbook"],
        "seq": rec["seq"],
        "digest": rec["digest"],
        "uri": f"axiom://attest/sha256:{rec['digest']}",
    }
    if result.anchor is not None:
        value["anchor_id"] = result.anchor["anchor_id"]
    return SkillResult(
        ok=True,
        value=value,
        actions_taken=[f"signed {rec['logbook']} #{rec['seq']} at {rec['site_id']}"],
    )


def new(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Write and sign an entry in one step."""
    if ctx is None or ctx.user_prompt is None:
        return fail("signing needs a person at the prompt; nothing was written")
    site, err = resolve_site(params)
    if err:
        return fail(err)
    for key in ("logbook", "type", "meaning", "title"):
        if not params.get(key):
            return fail(f"{key} is required")
    try:
        fields = parse_fields(params.get("fields"))
        content = {"title": params["title"], "fields": fields}
        if params.get("body"):
            content["body"] = params["body"]
        draft_id = service.create_draft(
            site_id=site,
            logbook=params["logbook"],
            entry_type=params["type"],
            meaning=params["meaning"],
            content=content,
            origin="human",
            for_principal=ctx.principal.handle,
            occurred_at=params.get("occurred_at") or None,
        )
    except (AttestRefused, ValueError) as exc:
        return fail(str(exc))
    return _respond(draft_id, site, ctx)


def sign(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Complete and sign a draft someone or something proposed for you."""
    draft_id = params.get("draft_id")
    if not draft_id:
        return fail("draft_id is required")
    try:
        d = service.draft(draft_id)
        if params.get("logbook") and params["logbook"] != d["logbook"]:
            from .. import registry

            try:
                book = registry.get(d["logbook"])
            except LogbookError:  # an undeclared log is named by its id
                book = None
            handle = (book.handle if book is not None else "") or d["logbook"]
            return fail(
                f"draft {draft_id} is in the log {handle!r}, not {params['logbook']!r}; "
                f"sign it with: log {handle} sign {draft_id}"
            )
        site, err = resolve_site({"site": d["site_id"]})
        if err:
            return fail(err)
        values = parse_fields(params.get("fields"))
        if values:
            who = Signatory(principal=ctx.principal, roles=(), display=ctx.principal.handle)
            service.fill(draft_id, values, by=who)
    except (AttestRefused, ValueError) as exc:
        return fail(str(exc))
    return _respond(draft_id, site, ctx)
