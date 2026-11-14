# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Export-control review: who releases a held file, and the trail every decision leaves (ADR-213 D9).

The hook in :mod:`.extract.ec` **holds** a file: stored, listed, filed, its text
kept out of retrieval. This module is the other half, the review:

**Who reviews.** A per-site ``ec-reviewer`` role. A person holds it at a site
when any of these says so:

* the site's role assignments (``axi log role grant <handle> <site>
  ec_reviewer``; the attest role grammar spells the role ``ec_reviewer``),
  the same per-site store and change log the logbooks' signing roles use;
* the deployment's OpenFGA model, when one is configured: the ``ec_reviewer``
  relation on ``site:<id>``, which the site owner satisfies (``authz/fga/site.fga``);
* the site's configuration, ``AXIOM_LIBRARY_EC_REVIEWERS`` (comma-separated handles).

When none of them names anybody at the site, the **owner is the default
reviewer**: the node's owner (its identity's ``owner``, or the local operator).

**What nobody can do.** The person who uploaded a file can never release it,
whatever roles they hold. The release is somebody else's decision.

**What a decision is.** Every hold, release, keep-held and re-hold is one
record: who, which file version (``sha256``), the code and reasons the hold
rests on (``basis``), and a required one-line reason from the person. Records
are appended to a per-site hash chain and sealed with the node signer
(``axiom.attest.chain.seal``), and each is also a library event, so the item's
own history shows it. A release that cannot be sealed is refused: an
unattested release would be a release nobody can prove.

**What a release covers.** One file version for one basis. A new version of the
file (different bytes) is a different item and is checked from scratch; if an
earlier version was released, the hold says so. If a re-extraction finds a
different reason to hold the same bytes, the release no longer covers it and
the file is re-held automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

REVIEWER_ROLE = "ec-reviewer"
#: The role id in the per-site role store (``[a-z0-9_]+``).
ROLE_ID = "ec_reviewer"
#: The FGA relation on ``site:<id>``.
FGA_RELATION = "ec_reviewer"
REVIEWERS_ENV = "AXIOM_LIBRARY_EC_REVIEWERS"
SYSTEM = "@library:system"
MAX_REASON = 500

ACTIONS = ("hold", "release", "keep-held", "re-hold")


class ReviewRefused(PermissionError):
    """The caller may not make this decision (or it cannot be attested)."""


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ reviewers


def _norm(handle: str) -> str:
    return (handle or "").strip().lower()


def _split(handle: str) -> tuple[str, str]:
    name, _, ctx = _norm(handle).lstrip("@").partition(":")
    return name, ctx


def _state_dir():
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def _role_holders(site: str) -> list[str]:
    try:
        from axiom.extensions.builtins.attest import roles

        return [a["principal"] for a in roles.assignments(site, state_dir=_state_dir()) if ROLE_ID in a["roles"]]
    except Exception as exc:  # noqa: BLE001 - an unreadable role file names nobody
        log.warning("library: ec-reviewer roles unreadable: %s", exc)
        return []


def _configured() -> list[str]:
    return [h.strip() for h in (os.environ.get(REVIEWERS_ENV) or "").split(",") if h.strip()]


def _fga_allows(principal: str, site: str) -> bool:
    try:
        from axiom.extensions.builtins.authz.openfga_http import client_from_config, resolve_config

        cfg = resolve_config()
        if cfg is None:
            return False
        client = client_from_config(cfg)
        return bool(client.check(user=f"user:{principal}", relation=FGA_RELATION, object=f"site:{site}"))
    except Exception as exc:  # noqa: BLE001 - FGA down means FGA grants nothing, never everything
        log.info("library: FGA ec-reviewer check unavailable: %s", exc)
        return False


def owners() -> list[str]:
    """The node's owner: its identity's ``owner`` and the local operator."""
    out = []
    try:
        from axiom.vega.federation.identity import load_identity

        ident = load_identity()
        if ident is not None and ident.owner:
            out.append(ident.owner)
    except Exception:  # noqa: BLE001
        pass
    try:
        from axiom.infra.principal import local_handle

        out.append(local_handle())
    except Exception:  # noqa: BLE001
        pass
    return out


def _matches(principal: str, handle: str, site: str) -> bool:
    if _norm(principal) == _norm(handle):
        return True
    p_name, p_ctx = _split(principal)
    h_name, h_ctx = _split(handle)
    # A bare owner name (``bbooth``) matches that person at this site or on this machine.
    return bool(p_name) and p_name == h_name and not h_ctx and p_ctx in ("", "local", site)


def reviewers(site: str) -> dict[str, Any]:
    """Who reviews at ``site``, and where that comes from (for display; FGA grants are checked, not listed)."""
    named = sorted(set(_role_holders(site)) | set(_configured()))
    return {
        "role": REVIEWER_ROLE,
        "site": site,
        "reviewers": named or owners()[:1],
        "default_owner": not named,
        "fga": FGA_RELATION,
    }


def is_reviewer(principal: str, site: str) -> bool:
    named = list(set(_role_holders(site)) | set(_configured()))
    if any(_matches(principal, h, site) for h in named):
        return True
    if _fga_allows(principal, site):
        return True
    return not named and any(_matches(principal, h, site) for h in owners())


def may_review(principal: str, item: Any) -> tuple[bool, str]:
    """Whether ``principal`` may release or keep-hold ``item``, and why not."""
    if _norm(principal) == _norm(item.owner):
        return False, "the person who uploaded a file cannot release it; another ec-reviewer must"
    if not is_reviewer(principal, item.site):
        return False, f"releasing a held file needs the {REVIEWER_ROLE} role at {item.site}"
    return True, ""


# ---------------------------------------------------------------- the trail


def basis(verdict: dict[str, Any]) -> str:
    """What a hold rests on, as a digest: the same code, reasons and markers give the same basis."""
    body = {
        "code": verdict.get("code") or "",
        "reasons": sorted(verdict.get("reasons") or []),
        "markers": sorted(verdict.get("markers") or []),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _clean_reason(reason: str | None) -> str:
    text = " ".join((reason or "").split())
    if not text:
        raise ValueError("a reason is required: one line saying why")
    if len(text) > MAX_REASON:
        raise ValueError(f"keep the reason to one line (at most {MAX_REASON} characters)")
    return text


def history(session: Any, item: Any) -> list[Any]:
    from axiom.extensions.builtins.library.db_models import LibraryEcReview

    return (
        session.query(LibraryEcReview)
        .filter(LibraryEcReview.item_id == item.id)
        .order_by(LibraryEcReview.seq)
        .all()
    )


def latest(session: Any, item: Any) -> Any:
    """The latest decision on this item's current bytes."""
    from axiom.extensions.builtins.library.db_models import LibraryEcReview

    return (
        session.query(LibraryEcReview)
        .filter(LibraryEcReview.item_id == item.id, LibraryEcReview.sha256 == item.sha256)
        .order_by(LibraryEcReview.seq.desc())
        .first()
    )


def record(
    session: Any,
    item: Any,
    action: str,
    principal: str,
    reason: str,
    verdict: dict[str, Any],
    *,
    surface: str = "",
    require_signature: bool = False,
    **detail: Any,
) -> Any:
    """Append one sealed decision to the site's chain, and the matching library event."""
    from axiom.attest.canonical import digest as canonical_digest
    from axiom.attest.chain import GENESIS, seal
    from axiom.extensions.builtins.library.db_models import LibraryEcReview, LibraryEvent

    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    last = (
        session.query(LibraryEcReview)
        .filter(LibraryEcReview.site == item.site)
        .order_by(LibraryEcReview.seq.desc())
        .first()
    )
    seq = (last.seq + 1) if last is not None else 1
    prev = last.digest if last is not None else GENESIS
    at = _now()
    body = {
        "kind": "library-ec-review",
        "action": action,
        "site": item.site,
        "item_id": item.id,
        "file_name": item.file_name,
        "sha256": item.sha256,
        "principal": principal,
        "reason": reason,
        "code": verdict.get("code") or "",
        "reasons": list(verdict.get("reasons") or []),
        "basis": basis(verdict),
        "at": at.isoformat(),
        **{k: v for k, v in detail.items() if v not in (None, "", [], {})},
    }
    signed = False
    try:
        from axiom.extensions.builtins.attest import signing

        sealed = seal(body, seq=seq, prev_digest=prev, signer=signing.signer())
        signed = True
    except Exception as exc:  # noqa: BLE001 - say why; a hold is still recorded, a release is not
        if require_signature:
            raise ReviewRefused(f"this decision cannot be attested on this node ({exc}); nothing was changed") from exc
        sealed = {**body, "seq": seq, "prev_digest": prev}
        sealed["digest"] = canonical_digest(sealed)
        sealed["unsigned"] = str(exc)[:300]
    row = LibraryEcReview(
        site=item.site, seq=seq, item_id=item.id, sha256=item.sha256, action=action, principal=principal,
        reason=reason, code=body["code"], basis=body["basis"], digest=sealed["digest"], record=sealed,
        signed=signed, at=at,
    )
    session.add(row)
    session.add(
        LibraryEvent(
            item_id=item.id, site=item.site, principal=principal, action=f"ec-{action}", surface=surface,
            detail={"reason": reason, "code": body["code"], "sha256": item.sha256, "seq": seq,
                    "digest": sealed["digest"], "signed": signed, **{k: v for k, v in detail.items() if v}},
            at=at,
        )
    )
    session.flush()
    return row


def review_json(row: Any) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "action": row.action,
        "principal": row.principal,
        "reason": row.reason,
        "code": row.code,
        "sha256": row.sha256,
        "at": row.at.isoformat() if row.at else None,
        "digest": row.digest,
        "signed": bool(row.signed),
        "key_id": ((row.record or {}).get("node_sig") or {}).get("key_id", ""),
    }


# ---------------------------------------------------- the pipeline's question


def _earlier_release(session: Any, item: Any) -> dict[str, Any] | None:
    """A released earlier version of this file (same name at the site, other bytes)."""
    from axiom.extensions.builtins.library.db_models import LibraryEcReview, LibraryItem

    q = (
        session.query(LibraryEcReview, LibraryItem)
        .join(LibraryItem, LibraryItem.id == LibraryEcReview.item_id)
        .filter(
            LibraryEcReview.site == item.site,
            LibraryEcReview.action == "release",
            LibraryItem.file_name == item.file_name,
            LibraryEcReview.sha256 != item.sha256,
        )
        .order_by(LibraryEcReview.seq.desc())
        .first()
    )
    if q is None:
        return None
    rev, prior = q
    return {"item_id": prior.id, "sha256": rev.sha256, "released_by": rev.principal,
            "released_at": rev.at.isoformat() if rev.at else None}


def on_verdict(session: Any, item: Any, verdict: dict[str, Any]) -> dict[str, Any]:
    """The verdict the item carries after review: a covering release turns ``held`` into ``released``.

    Records the automatic decisions: a first hold, and a re-hold when a
    release no longer covers what the hold now rests on.
    """
    if verdict.get("verdict") != "held":
        return verdict
    b = basis(verdict)
    last = latest(session, item)
    if last is not None and last.action == "release" and last.basis == b:
        return {**verdict, "verdict": "released", "held": dict(verdict), "release": review_json(last)}
    if last is not None and last.action == "release":
        record(session, item, "re-hold", SYSTEM,
               "re-checked: what the hold rests on changed after the release, so the release no longer covers it",
               verdict, surface="pipeline")
    elif last is None or last.basis != b:
        earlier = _earlier_release(session, item)
        if earlier is not None:
            verdict = {**verdict, "reasons": [
                *(verdict.get("reasons") or []),
                "an earlier version of this file was released; a release covers one version, "
                "so this one is held until it is reviewed",
            ]}
        record(session, item, "hold", SYSTEM, "; ".join(verdict.get("reasons") or [])[:MAX_REASON] or "held",
               verdict, surface="pipeline", earlier_version=earlier)
    return verdict


# ------------------------------------------------------------------ decisions


def _held_verdict(item: Any) -> dict[str, Any]:
    ec = dict(item.ec or {})
    return dict(ec.get("held") or ec) if ec.get("verdict") == "released" else ec


def state(caller: Any, item_id: str) -> dict[str, Any]:
    """The review state of one item, and what this caller may do about it."""
    from axiom.extensions.builtins.library.service import _visible
    from axiom.extensions.builtins.library.store import session_scope

    with session_scope() as session:
        item = _visible(session, caller, item_id)
        ec = dict(item.ec or {})
        ok, why = may_review(caller.principal, item)
        verdict = ec.get("verdict") or "clear"
        return {
            "item_id": item.id,
            "sha256": item.sha256,
            "verdict": verdict,
            "code": ec.get("code") or (ec.get("held") or {}).get("code") or "",
            "kind": ec.get("kind") or (ec.get("held") or {}).get("kind") or "",
            "file_kind": ec.get("file_kind") or (ec.get("held") or {}).get("file_kind") or "",
            "reasons": list(_held_verdict(item).get("reasons") or []),
            "uploader": item.owner,
            "can_release": verdict == "held" and ok,
            "can_keep_held": verdict == "held" and ok,
            "can_rehold": verdict == "released" and (
                _norm(caller.principal) == _norm(item.owner) or is_reviewer(caller.principal, item.site)),
            "why_not": "" if ok else why,
            "reviewers": reviewers(item.site),
            "history": [review_json(r) for r in history(session, item)],
        }


def release(caller: Any, item_id: str, reason: str) -> dict[str, Any]:
    """An ec-reviewer releases one held file version; its text is indexed."""
    from axiom.extensions.builtins.library import pipeline
    from axiom.extensions.builtins.library.service import Forbidden, _visible
    from axiom.extensions.builtins.library.store import session_scope

    text = _clean_reason(reason)
    with session_scope() as session:
        item = _visible(session, caller, item_id)
        ec = dict(item.ec or {})
        if ec.get("verdict") != "held":
            raise ValueError(f"{item.file_name!r} is not held (it is {ec.get('verdict') or 'clear'})")
        ok, why = may_review(caller.principal, item)
        if not ok:
            raise Forbidden(why)
        try:
            row = record(session, item, "release", caller.principal, text, ec, surface=caller.surface,
                         require_signature=True)
        except ReviewRefused as exc:
            session.rollback()
            raise Forbidden(str(exc)) from exc
        item.ec = {**ec, "verdict": "released", "held": ec, "release": review_json(row)}
        item.index_status, item.index_reason = "pending", "released by an export-control reviewer; indexing"
        item.pipeline_state = "done"
        item.updated_at = _now()
        session.commit()
        has_doc = (pipeline.derived_dir(item) / "rag.md").exists()
    note = pipeline.embed(item_id) if has_doc else pipeline.reindex_after_release(item_id)
    out = state(caller, item_id)
    out["indexed"] = note
    return out


def keep_held(caller: Any, item_id: str, reason: str) -> dict[str, Any]:
    """An ec-reviewer looked and keeps the file held; the decision and its reason are recorded."""
    from axiom.extensions.builtins.library.service import Forbidden, _visible
    from axiom.extensions.builtins.library.store import session_scope

    text = _clean_reason(reason)
    with session_scope() as session:
        item = _visible(session, caller, item_id)
        ec = dict(item.ec or {})
        if ec.get("verdict") != "held":
            raise ValueError(f"{item.file_name!r} is not held (it is {ec.get('verdict') or 'clear'})")
        ok, why = may_review(caller.principal, item)
        if not ok:
            raise Forbidden(why)
        row = record(session, item, "keep-held", caller.principal, text, ec, surface=caller.surface)
        item.ec = {**ec, "kept": review_json(row)}
        item.updated_at = _now()
        session.commit()
    return state(caller, item_id)


def rehold(caller: Any, item_id: str, reason: str) -> dict[str, Any]:
    """Take a release back: the file's text leaves retrieval and it is held again.

    Holding is the safe direction, so an ec-reviewer or the file's uploader may.
    """
    from axiom.extensions.builtins.library.service import Forbidden, _visible
    from axiom.extensions.builtins.library.store import rag_store, session_scope

    text = _clean_reason(reason)
    with session_scope() as session:
        item = _visible(session, caller, item_id)
        ec = dict(item.ec or {})
        if ec.get("verdict") != "released":
            raise ValueError(f"{item.file_name!r} is not released")
        if _norm(caller.principal) != _norm(item.owner) and not is_reviewer(caller.principal, item.site):
            raise Forbidden(f"re-holding needs the {REVIEWER_ROLE} role at {item.site}, or to be the uploader")
        held = dict(ec.get("held") or {**ec, "verdict": "held"})
        record(session, item, "re-hold", caller.principal, text, held, surface=caller.surface)
        item.ec = {**held, "verdict": "held"}
        store = rag_store()
        if store is not None:
            try:
                store.delete_document(item.source_path, corpus=item.corpus)
            except Exception as exc:  # noqa: BLE001 - the status says held; the store is retried by a re-extract
                log.warning("library: removing %s from retrieval failed: %s", item.id, exc)
        item.index_status, item.index_reason, item.chunk_count = "held", "re-held: " + text, 0
        item.pipeline_state = "held"
        item.updated_at = _now()
        session.commit()
    return state(caller, item_id)


__all__ = [
    "ACTIONS",
    "REVIEWERS_ENV",
    "REVIEWER_ROLE",
    "ReviewRefused",
    "basis",
    "history",
    "is_reviewer",
    "keep_held",
    "may_review",
    "on_verdict",
    "record",
    "release",
    "rehold",
    "reviewers",
    "state",
]
