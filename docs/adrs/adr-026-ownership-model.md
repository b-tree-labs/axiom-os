# ADR-026: Memory Ownership Model

**Status:** Accepted (as-implemented)
**Date:** 2026-04-17
**Authors:** Benjamin Booth, Claude
**Related:** ADR-022/023/024/025 (federation), `project_memory_architecture_unified.md`, ADR-027 (federated memory), ADR-028 (trust graph).
**Implementation:** `src/axiom/memory/ownership.py` (34 tests passing).

---

## Purpose

Establish **ownership** as a first-class dimension of Axiom memory,
distinct from access (visibility) and scope (logical location).
Ownership answers *"who is the authoritative controller?"*

Without this ADR, Axiom had access control (bipartite graphs,
classification gates) and scope (cognitive types, retention tiers)
but no model for *who can delete/transfer/delegate*. Federation
makes this gap acute: a fragment cached on a peer node still has
a canonical owner, and the peer's obligations flow from that
ownership record, not from local bits.

---

## Decision

### Model

**Single master + peer delegations.** Every memory fragment has at
most one master principal. The master is the authoritative
controller. Delegations grant subsets of rights to other principals
(agents, sub-agents, collaborators). No co-ownership — prevents
deadlock and simplifies conflict semantics.

### Four independently delegatable rights

| Right | Capability |
|---|---|
| `CONTROL` | delete / modify / quarantine / revoke |
| `GOALS` | set what future actions do with the memory |
| `RESOURCES` | compute / storage / network budget allocation |
| `EFFORT` | direct agent cycles and attention |

An agent may be granted `EFFORT` only (it can work on the memory
but can't redirect its goals). A collaborator may get `GOALS` only
(can propose purposes but can't delete).

### Time-bounded delegations

All delegations MUST have an `expires_at`. No open-ended grants.
Forces periodic renewal, ensures that forgotten delegations
eventually lapse, and matches how professional trust actually
works (TA appointments are terms; not perpetuities).

### Transfer is a clean break

Transfer of ownership clears all prior delegations. The new master
starts fresh. Requires:
- **Outgoing signature** from the prior master (consent; blocks
  unilateral dumping).
- **Incoming acceptance** from the new master (prevents
  unwilling-recipient scenarios, like content being pushed onto a
  cohort that didn't ask for it).

Use cases: student graduation (harvest becomes their own),
researcher role retirement (curriculum transfers to successor),
institutional deprecation (dataset transfers to archive steward).

### Trust target decomposition

Trust records target a `(principal, role, context)` triple, not
just a principal:

```
TrustTarget {
    principal: Optional[str]   # the human (None = role-only trust)
    role: Optional[str]        # the role they hold (None = human-only)
    context: str               # domain/maturity/classification bundle
}
```

This lets role succession rebind role-scoped trust to the new
occupant without touching human-scoped trust. Adversarial
replacement (rogue admin "fires" incumbent, "hires" malicious
successor) is blocked by the signed-outgoing-consent requirement
in the succession ceremony.

### Ownership travels with the fragment

Federation peers that cache a fragment inherit obligations, not
ownership. Tombstones propagate along the federation trust edges;
a revocation by the owner invalidates all replicas.

---

## Rationale

### Why single master

Co-ownership creates deadlock ("A wants to delete, B wants to
keep"). Real-world collaboration is usually *one-person-owns*
with explicit delegations — match the model to practice.

### Why four rights, not one

A single "owner can do anything" right forces over-delegation:
you hand your agent full control to let it do one job. Splitting
rights lets you grant the minimum needed. Least-privilege at the
ownership layer.

The four rights were chosen by Ben's analysis of what "ownership"
actually means in practice: *control* (destructive), *goals*
(directional), *resources* (budget), *effort* (attention). They
compose orthogonally — you can grant any subset.

### Why time-bounded delegations

Permanent delegations accumulate into zombie rights — grantors
forget, grantees leave organizations, and the graph grows
unfalsifiable. Forced expiry surfaces dead delegations as part of
normal operations.

### Why transfer-not-re-delegate on graduation

Ben: "decentralize what you can, centralize only what you must."
A graduated student should own their own record. A re-delegation
keeps the institution as permanent master, which fights the
principle. Transfer hands them full authority.

### Why (principal, role, context)

Real trust in professional settings follows the role, not just the
human. A new air-traffic-controller manager must inherit the
trust of the role instantly; a new curriculum steward takes over
the curriculum trust without each downstream party re-rating them.
Human-only trust misses this. Role-only trust misses the "this
specific individual, not everyone in the role" case. The triple
covers both.

---

## Alternatives considered

| Alternative | Rejected because |
|---|---|
| Single `owner` field, no rights split | Forces all-or-nothing delegation; no least-privilege |
| Co-ownership (M-of-N masters) | Deadlock-prone; hard to reason about revocation |
| Role only (no human in trust target) | Misses "trust this specific person" cases |
| Human only (no role) | Misses succession, breaks on turnover |
| Permanent delegations | Zombie authorities; audit nightmare |
| One-sided transfer (outgoing signs; no acceptance) | Enables unwanted-recipient attacks |

---

## Consequences

### Positive

- **Ownership is auditable.** Every fragment carries its master id;
  delegations are signed; transfers are ceremonies.
- **Federation obligations are clear.** A peer honors the owner's
  revocations because ownership travels, not just the bits.
- **Least-privilege delegation** supports agent architectures:
  each agent gets exactly the rights needed.
- **Role succession is a first-class operation.** New occupants
  inherit role-scoped trust atomically; old occupants' human-scoped
  trust stays with them.
- **Graduation model works naturally.** Transfer, not re-delegation.

### Negative

- **Four rights add cognitive load** for developers (`Right.GOALS`
  vs `Right.RESOURCES` vs ...). Mitigated by `all_rights()` helper
  and clear docstrings.
- **Forced expiry requires renewal workflows.** Classroom
  delegations need refresh at semester rollover. Tolerable — most
  delegations naturally align with semester boundaries.
- **Transfer ceremony is a two-signature protocol.** Slightly
  more complex than "update field", but required for adversarial
  resistance.

### Migration

Fragments without an `ownership` field are treated as
**anonymously-owned** — the platform's lowest-trust tier. Existing
fragments (pre-ADR-026) stay functional; new code should set
ownership at creation time.

---

## Open items

- **Posthumous principals.** What happens to content when the
  master principal is decommissioned / deceased? Current default:
  delegated-to-institution via succession ceremony; opt-out is
  required. Defer detailed policy to a separate ADR if this
  becomes load-bearing.
- **Group principal membership changes.** `@ut-nuclear-faculty`
  gains a new faculty member — does existing delegated authority
  auto-extend? Current answer: yes, via role-scoped trust; ADR-028
  handles this.
- **Delegation depth limit.** An agent sub-delegates to a sub-agent
  that sub-delegates to... Current implementation allows any
  depth (monotonically decreasing rights). Cap if abuse emerges.

---

## Test coverage

`tests/memory/test_ownership.py`: 20 tests covering rights enum,
ownership construction/immutability, delegation grant/revoke,
`can_exercise` authorization check, transfer ceremony (including
signature requirements + delegation clearing), `TrustTarget`
shape, fragment integration, role succession ceremony.

Also: `tests/memory/test_fragment.py` updated; fragment's
`ownership` slot is now the canonical home for this data.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
