# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CompositionService — unified entry point for every memory operation.

Every write and read in Axiom flows through this service so that every
primitive (policy, access, gating, attestation, persistence, audit) is
consulted on every operation. No call sites bypass the stack. That is
the architectural contract that makes v1 "production" a defensible
claim rather than a marketing phrase.

Design:
- Accepts every primitive as a dependency at construction. Downstream
  extensions bootstrap one service per classroom / federation / tenant.
- write() resolves policy → routes write scope → applies transform →
  signs → persists → records audit entry.
- read() loads from registry → filters through access check + signature
  verification → records audit entry.
- llm_response() runs post-filter breach detection before emit.

Optional primitives (signing keypair, transform) degrade gracefully —
a service without a keypair simply emits unsigned fragments.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime

from axiom.artifacts.registry import ArtifactRegistry
from axiom.vega.identity.keypair import Keypair

from .access import AccessGraphs, is_visible
from .attest import AuditLog, sign_fragment, verify_fragment_signature
from .exceptions import AccountabilityError
from .fragment import (
    MemoryFragment,
    Provenance,
    SourceOrigin,
    create_fragment,
    fragment_from_dict,
)
from .ownership import Ownership, Right, can_exercise, new_ownership
from .policy import PolicyCoord
from .post_filter import BreachCheckResult, check_llm_output
from .trust import TrustGraph
from .write_policy import WriteScope, scope_from_policy

_UNSET = object()


def _validate_accountable_human(
    value: object, *, principal_id: str, frag_id: str,
) -> str:
    """Reject writes that lack a real accountable human.

    Per ADR-035 §D1: every memorable action MUST be bound to a named
    human. Resolution rules:

    - Explicit empty string → reject. Caller passed an explicitly-empty
      value, signaling they have not bound the fragment.
    - Explicit ``legacy:*`` prefix → reject. The legacy sentinel is
      read-back-only; new writes carry a real principal.
    - Unset / ``None`` → fall back to ``principal_id`` per ADR-035 §D1
      "When a human acts directly, accountable_human_id == principal_id".
      The fallback still validates the resulting value (so an empty
      principal_id still raises). Extension-level sweeps to set the
      field explicitly remain a tracked follow-on (ADR-035 §D8).
    """
    if value is _UNSET or value is None:
        # Inherit from the actor — the human-acts-directly default.
        value = principal_id
    if value == "":
        raise AccountabilityError(
            f"fragment {frag_id} missing accountable_human_id"
        )
    if not isinstance(value, str):
        raise AccountabilityError(
            f"fragment {frag_id} accountable_human_id must be a string"
        )
    if value.startswith("legacy:"):
        raise AccountabilityError(
            f"fragment {frag_id} cannot be written with legacy "
            f"accountable_human_id={value!r}; assign a real principal "
            f"or run `axi memory migrate --backfill-accountable-human`"
        )
    return value


@dataclass(frozen=True)
class ForgetResult:
    """Outcome of a :meth:`CompositionService.forget` call.

    Ids are partitioned so a bulk purge is self-reporting: what was
    redacted, what the requester lacked control over, and what no longer
    existed.
    """

    forgotten: list[str]
    denied: list[str]
    not_found: list[str]

    @property
    def count(self) -> int:
        return len(self.forgotten)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class CompositionService:
    """Single entry point for all memory operations.

    Construction wires every primitive. Extensions build one service per
    tenant / classroom / federation; operations then compose through it.
    """

    artifact_registry: ArtifactRegistry
    audit_log: AuditLog
    signing_keypair: Keypair | None
    policy_coord: PolicyCoord
    access_graphs: AccessGraphs
    trust_graph: TrustGraph
    transform: callable | None = None  # write-policy transform for shared tier
    # ADR-087 D5: optional rag-memory recall index (axiom.memory.recall
    # .RecallIndex). None = recall unavailable; reads/writes unaffected.
    recall_index: object | None = None

    # ------- Write path -----------------------------------------------------

    def write(
        self,
        content: dict,
        cognitive_type: str,
        principal_id: str,
        agents: set[str],
        resources: set[str],
        ownership: Ownership | None = None,
        at: str | None = None,
        *,
        accountable_human_id: object = _UNSET,
        delegation_chain: tuple[str, ...] = (),
        session_id: str | None = None,
        origin: SourceOrigin | None = None,
    ) -> MemoryFragment:
        """Write a MemoryFragment through the full stack.

        1. Construct the fragment with (T, U, A, R) provenance.
        2. Attach ownership (default: master = principal_id).
        3. Resolve policy → pick write scope.
        4. If shared + transform: apply transformation.
        5. Sign with keypair (if configured).
        6. Persist as Artifact in registry.
        7. Record audit entry.

        Per ADR-035 §D1: ``accountable_human_id`` is mandatory at write
        time. Empty strings and ``legacy:`` sentinel values are rejected
        with :class:`AccountabilityError` *before* any persistence.

        ``origin`` (ADR-087 D1/D2): the write-once source coordinate for
        absorbed/imported memories. ``None`` (default) = native. This is
        the single door through which the absorb import primitive lands
        origin-stamped fragments — adapters never write.
        """
        # Resolve session_id from the active session manager when not
        # supplied. Empty session_id is the legacy / no-session-active
        # case and is still a valid write (read paths treat it per
        # spec-memory §3.7.3).
        resolved_session = session_id
        if resolved_session is None:
            from .session import current_session_id
            resolved_session = current_session_id()

        # 1. Fragment with provenance
        frag = create_fragment(
            content=content,
            cognitive_type=cognitive_type,
            principal_id=principal_id,
            agents=agents,
            resources=resources,
            session_id=resolved_session,
        )

        # 1a. Accountability binding (ADR-035) — fail before persistence.
        validated_human = _validate_accountable_human(
            accountable_human_id,
            principal_id=principal_id,
            frag_id=frag.id,
        )
        frag = dataclasses.replace(
            frag,
            provenance=Provenance(
                timestamp=frag.provenance.timestamp,
                principal_id=frag.provenance.principal_id,
                agents=frag.provenance.agents,
                resources=frag.provenance.resources,
                accountable_human_id=validated_human,
                delegation_chain=tuple(delegation_chain),
                session_id=resolved_session,
                origin=origin if origin is not None else frag.provenance.origin,
            ),
        )

        # 2. Ownership
        own = ownership if ownership is not None else new_ownership(master=principal_id)
        frag = dataclasses.replace(frag, ownership=own)

        # 3. Policy → scope
        at = at or frag.provenance.timestamp
        scope = scope_from_policy(
            self.policy_coord,
            user=principal_id,
            agent=next(iter(agents)) if agents else "system",
            at=at,
        )

        # 4. Transform on shared tier
        if scope is WriteScope.SHARED and self.transform is not None:
            frag = self.transform(frag)

        # 5. Signing
        if self.signing_keypair is not None:
            frag = sign_fragment(frag, self.signing_keypair)

        # 6. Persist
        self.artifact_registry.register(
            kind="fragment",
            name=frag.id,
            data=frag.to_dict(),
        )

        # 7. Audit
        self.audit_log.record(
            entry_type="write",
            principal_id=principal_id,
            agent_id=next(iter(agents)) if agents else "system",
            fragment_id=frag.id,
            outcome="ok",
            scope=scope.value,
        )

        # 8. Recall projection (best-effort — the index is a rebuildable
        # read-side structure; its failure never fails the write).
        if self.recall_index is not None:
            try:
                self.recall_index.index_fragment(frag)
            except Exception as exc:  # noqa: BLE001
                self.audit_log.record(
                    entry_type="recall_index_error",
                    principal_id=principal_id,
                    agent_id=next(iter(agents)) if agents else "system",
                    fragment_id=frag.id,
                    outcome=str(exc)[:200],
                )

        return frag

    # ------- Read path ------------------------------------------------------

    def read(
        self,
        fragment_ids: list[str],
        user: str,
        agent: str,
        at: str | None = None,
    ) -> list[MemoryFragment]:
        """Read fragments through access check + signature verification.

        Missing ids are silently skipped. Denials are audit-logged as
        read_denied so revocation-time-of-flight is reconstructible.
        """
        results: list[MemoryFragment] = []
        for fid in fragment_ids:
            # Load — keyed (kind, name) lookup, not a kind-wide scan
            # (ADR-087 P1). Ordering matches the old scan: created_at
            # ascending, tombstones excluded.
            artifacts = self.artifact_registry.find_by_name("fragment", fid)
            if not artifacts:
                continue
            frag = fragment_from_dict(artifacts[0].data)

            # Access check
            if not is_visible(self.access_graphs, user=user, agent=agent, fragment=frag):
                self.audit_log.record(
                    entry_type="read_denied",
                    principal_id=user,
                    agent_id=agent,
                    fragment_id=fid,
                    outcome="denied_by_access",
                )
                continue

            # Signature verification (if signed)
            if frag.signature is not None and self.signing_keypair is not None:
                if not verify_fragment_signature(
                    frag, self.signing_keypair.public_bytes
                ):
                    self.audit_log.record(
                        entry_type="read_denied",
                        principal_id=user,
                        agent_id=agent,
                        fragment_id=fid,
                        outcome="signature_invalid",
                    )
                    continue

            results.append(frag)
            self.audit_log.record(
                entry_type="read",
                principal_id=user,
                agent_id=agent,
                fragment_id=fid,
                outcome="ok",
            )
        return results

    # ------- Recall path (ADR-087 D5) ----------------------------------------

    def recall(
        self,
        query: str,
        *,
        user: str,
        agent: str,
        principal: str | None = None,
        intent: str = "lookup",
        k: int = 5,
        cognitive_types: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        recency_bias: float | None = None,
    ):
        """Semantic search over a principal's own memory.

        Routes through the hybrid retriever (dense + sparse → RRF) over
        the ADR-088 ``rag-memory`` corpus, then resolves every hit back
        through :meth:`read` — access checks, signature verification,
        and tombstone exclusion apply to recall results exactly as to
        direct reads, even against a stale index. ``read()`` keeps its
        fetch-by-id semantics; this is the query-shaped sibling.

        ``principal`` defaults to ``user`` (recalling your own memory).
        Structured predicates: the principal scopes the corpus at the
        store; ``cognitive_types``/``since``/``until`` filter the
        candidate set before ranking. ``recency_bias`` defaults to the
        RPE plan parameter for ``intent``.
        """
        from .recall import RecallResult, rank_fragments, resolve_recency_bias
        from .recall_projection import recall_corpus_for

        if self.recall_index is None:
            raise RuntimeError(
                "no recall index configured on this CompositionService — "
                "construct it with recall_index=RecallIndex(...) to enable "
                "memory recall (ADR-087 P1)"
            )

        target = principal or user
        candidate_ids, rrf_scores, degraded = self.recall_index.search(
            query, principal=target, limit=max(k, 1) * 4,
        )

        fragments = self.read(candidate_ids, user=user, agent=agent)

        if cognitive_types:
            allowed = set(cognitive_types)
            fragments = [
                f for f in fragments if f.cognitive_type.value in allowed
            ]
        if since or until:
            def _time_of(f):
                event_time = f.content.get("event_time")
                if isinstance(event_time, str) and event_time:
                    return event_time
                return f.provenance.timestamp

            if since:
                fragments = [f for f in fragments if _time_of(f) >= since]
            if until:
                fragments = [f for f in fragments if _time_of(f) <= until]

        bias = resolve_recency_bias(intent, recency_bias)
        ranked, scores = rank_fragments(
            fragments, rrf_scores, recency_bias=bias, limit=k,
        )

        self.audit_log.record(
            entry_type="recall",
            principal_id=user,
            agent_id=agent,
            fragment_id="",
            outcome="ok",
            query=query[:200],
            results=len(ranked),
            degraded=degraded,
        )
        return RecallResult(
            fragments=ranked,
            scores=scores,
            degraded=degraded,
            corpus=recall_corpus_for(target),
            query=query,
        )

    # ------- Forget path ----------------------------------------------------

    def forget(
        self,
        fragment_ids: list[str],
        requester: str,
        agent: str,
        reason: str | None = None,
        at: str | None = None,
    ) -> ForgetResult:
        """Redact fragments from recall (soft-delete / tombstone).

        The first authorized memory mutation. For each id:

        1. resolve the live (non-tombstoned) artifact rows carrying it;
        2. require the requester to hold ``Right.CONTROL`` over the
           fragment's ownership — the master always does — else audit
           ``forget_denied`` and skip;
        3. tombstone every matching row via ``artifact_registry.delete``
           (``read``/``list``/recall exclude ``deleted=1`` by default) and
           record a ``forget`` audit entry.

        Redaction, not erasure: the row + ``deletion_reason`` are retained
        for audit and the frozen fragment is never mutated, so the
        immutable-provenance contract holds. Denied / unknown ids are
        collected in the result, never raised — a bulk purge does what it
        may and reports the rest.
        """
        resolved_at = at or datetime.now(UTC).isoformat()
        forgotten: list[str] = []
        denied: list[str] = []
        not_found: list[str] = []

        for fid in fragment_ids:
            artifacts = self.artifact_registry.find_by_name("fragment", fid)
            if not artifacts:
                not_found.append(fid)
                continue

            frag = fragment_from_dict(artifacts[0].data)
            own = frag.ownership or new_ownership(
                master=frag.provenance.principal_id
            )
            if not can_exercise(own, requester, Right.CONTROL, resolved_at):
                self.audit_log.record(
                    entry_type="forget_denied",
                    principal_id=requester,
                    agent_id=agent,
                    fragment_id=fid,
                    outcome="denied_by_ownership",
                )
                denied.append(fid)
                continue

            for a in artifacts:
                self.artifact_registry.delete(a.id, reason=reason or "forget")
            if self.recall_index is not None:
                try:
                    self.recall_index.evict(
                        fid, frag.provenance.principal_id
                    )
                except Exception as exc:  # noqa: BLE001
                    self.audit_log.record(
                        entry_type="recall_index_error",
                        principal_id=requester,
                        agent_id=agent,
                        fragment_id=fid,
                        outcome=str(exc)[:200],
                    )
            self.audit_log.record(
                entry_type="forget",
                principal_id=requester,
                agent_id=agent,
                fragment_id=fid,
                outcome="ok",
                reason=reason or "",
            )
            forgotten.append(fid)

        return ForgetResult(
            forgotten=forgotten, denied=denied, not_found=not_found
        )

    # ------- LLM response post-filter --------------------------------------

    def llm_response(
        self,
        output: str,
        user: str,
        agent: str,
        visible_fragments: list[MemoryFragment],
        all_fragments: list[MemoryFragment],
        min_quote_words: int = 10,
    ) -> BreachCheckResult:
        """Run post-filter breach detection on LLM output before emit.

        If breaches detected, record audit entries so leaks are
        reconstructible even when the output is still emitted (caller
        decides whether to block, redact, or pass).
        """
        result = check_llm_output(
            output=output,
            visible_fragments=visible_fragments,
            all_fragments=all_fragments,
            min_quote_words=min_quote_words,
        )
        if not result.is_clean:
            for breach in result.breaches:
                self.audit_log.record(
                    entry_type="post_filter_breach",
                    principal_id=user,
                    agent_id=agent,
                    fragment_id=breach.get("fragment_id", ""),
                    outcome=breach.get("reason", "unknown"),
                )
        return result
