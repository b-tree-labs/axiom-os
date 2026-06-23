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

from axiom.artifacts.registry import ArtifactRegistry
from axiom.vega.identity.keypair import Keypair

from .access import AccessGraphs, is_visible
from .attest import AuditLog, sign_fragment, verify_fragment_signature
from .exceptions import AccountabilityError
from .fragment import (
    MemoryFragment,
    Provenance,
    create_fragment,
    fragment_from_dict,
)
from .ownership import Ownership, new_ownership
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
            # Load
            artifacts = [
                a for a in self.artifact_registry.list(kind="fragment")
                if a.name == fid
            ]
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
