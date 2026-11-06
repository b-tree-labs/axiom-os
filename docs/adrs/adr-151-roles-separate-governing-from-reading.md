# ADR-151: A role separates governing from reading

**Status:** Accepted (2026-10-02)
**Related:** [ADR-026](adr-026-ownership-model.md) (ownership model),
[ADR-029](adr-029-federation-composition.md) (federation composition),
[ADR-045](adr-045-raci-evolution.md) (RACI, and what may act unattended),
[ADR-075](adr-075-sso-oidc-delegated-auth.md) (SSO, OIDC and delegated auth)

## Context

The gate grants access by **scope bundle**: a named role resolving to a
tuple of scopes, with a registry, and an override that is refused when it
would widen a grant. Two bundles ship:

```
admin   ("*",)        every mount, every governance verb
viewer  ("*:read",)   read-only on every mount
```

A scope is `<owning-extension>[:read|invoke|access]`, the verb taken from
the HTTP method, so `*` is read, write and govern fused into one grant.

That vocabulary cannot express two people we already have.

**An administrator who does not read.** Institutional IT staff administer a
node — accounts, roles, credentials, settings — and have no business
reading what the node holds. Under `admin = *` the only way to let somebody
rotate a key is to let them read every measurement, every memory
fragment and every audit record the node holds. The honest grant does not
exist, so the dishonest one gets issued.

**A builder who does not grant.** A colleague writing a monitor needs to
create, write and deploy. They do not need to invite people or issue
credentials. `viewer` is too little and `admin` is far too much, so in
practice the choice collapses to `admin`, and a workshop of six becomes six
unrestricted principals.

There is a third gap below `viewer`. A published surface — the public face
of a site — needs a tier that is *not* "read-only on everything", because
`*:read` includes audit, vault entries, memory and whatever mount is added
next week. Defined by subtraction, a public tier is wrong the moment
somebody mounts something new, and wrong silently.

This is the moment to fix it. Role names travel into invitations, into
issued credentials, into federation claims and into every audit record that
explains why somebody had access. Every cloud provider worth copying has
had to retrofit least privilege onto a convenient early `*`, and the
retrofit is expensive precisely because the grants are already out.

## Decision

### 1. Six roles, and governing is not reading

| Role | Reads | Creates and writes | Governs | Precedent |
|---|---|---|---|---|
| `owner` | yes | yes | yes, including appointing administrators | GCP Owner, Cloudflare Super Administrator |
| `admin` | no | no | yes | GCP Security Admin, AWS IAM-only access |
| `operator` | yes | yes | yes | Cloudflare Administrator |
| `creator` | yes | yes | no | AWS PowerUserAccess |
| `viewer` | yes | no | no | AWS ReadOnlyAccess, GCP Viewer |
| `guest` | published surfaces only | no | no | the public floor; ours |

`admin` reading nothing is the point of the row, not an oversight. An
administrator who needs to read something asks for `operator`, and the ask
is visible.

### 2. The scope grammar gains a governance half

A scope's resource half already names an owning extension. Governance is
not a different extension; it is a different *kind* of act on the same
mounts — issuing a credential, granting a role, changing a policy, reading
an audit trail. So the verb half gains `govern`:

```
<extension>[:read|invoke|govern|access|*]
```

`*` keeps its meaning and therefore keeps granting everything, which is why
`owner` holds it and nothing else does. `admin` is `*:govern`. `creator` is
`*:read` plus `*:invoke` and no `govern`. The method-to-verb map is
unchanged for read and invoke; a request is `govern` when it reaches a
route the owning extension declares as governing.

### 3. The floor is an allowlist, never a subtraction

`guest` resolves to the surfaces a deployment has **published**, named one
by one. A mount is not visible to `guest` until somebody adds it. There is
no form of `guest` that means "everything except"; the registry refuses a
`guest` bundle containing `*`.

### 4. A node is authoritative for its own principals

A peer's role grant is a **claim**, which the receiving node may accept,
narrow or refuse under its own policy. It is not a fact the node must
honour. The acceptance rule is a seam with a default of "accept nothing",
not a constant.

We do not yet know what carrying roles across a federation should mean.
This is deliberately the smaller promise: widening it later is a policy
change, while narrowing it after people depend on it is a migration and an
argument.

### 5. What a role is NOT

A role says what a principal may **do**. It does not say what they have
**agreed to**, and it does not say where an answer may **go**. Three
independent checks gate a request, and all three must pass:

| Axis | Question | Where it lives |
|---|---|---|
| Role and scope | may this principal perform this act on this mount? | this ADR |
| Agreement | does a live, unexpired, unrevoked attestation cover this content? | the access-agreements work |
| Destination | can the sink receiving the answer hold it? | the client capability gate |

The third is the one most easily forgotten. Export-controlled content is
withheld from a client whose model runs outside the enclave **even when the
person asking is authorised**, because authorisation is about a human and
the destination is about a machine. A role that implied either of the other
two axes would silently release content on the strength of the wrong fact.

Two prohibitions follow, and both are the kind that get violated by
somebody being helpful:

- **An agreement is never expressed as a role.** No `ec-viewer`, no
  `nda-creator`. That collapse produces a role per combination of
  agreements, and leaves no way to say "their agreement expired" short of
  deleting a role.
- **A role never implies a destination capability.** Granting `operator`
  does not make a cloud harness able to receive controlled output.

### 6. What never graduates to an agent

Per ADR-045 D6, trust graduates over the undoable and never over the
irreversible. Granting `admin`, `operator` or `owner`, publishing a surface
to `guest`, and accepting a peer's claim are **irreversible in effect** —
what the grantee reaches during the window cannot be recalled — and
therefore never graduate past `C`, whatever an agent has earned.

## Consequences

**A role means less than it did, and says so.** Anyone holding `admin`
today holds `*`. Migrating them to `*:govern` removes read access they
currently have. That is the intended correction, and it has to be a
deliberate, announced migration rather than a silent redefinition, because
somebody's working habit depends on it.

**Three things must be built before invitations can carry a role**: the
`govern` verb in the scope grammar, the four new bundles, and a declaration
by each extension of which of its routes govern. The last is the real work:
until an extension declares, its governing routes are indistinguishable
from its writing routes and `admin` cannot be enforced for it.

**`guest` costs a publishing step.** Nothing reaches the floor tier by
accident, which is the property we want and also a chore: a surface meant
to be public stays invisible until somebody publishes it, and the person
who mounts it is usually not the person who notices.

**Federation stays narrow.** Until the acceptance rule is written, a
federated peer's users hold nothing here. A cohort that expects shared
access will find it does not work yet, which is better than finding it
works differently than they assumed.

**Three axes mean three ways to be refused, and a refusal has to say
which.** "You may not do that", "you have not signed that" and "this cannot
be sent where you are asking from" are different sentences with different
remedies, and a single generic denial would send people to the wrong one.

**The audit record gains meaning.** "Granted `creator`" is a sentence about
what somebody can do. "Granted `admin`" previously was not.
