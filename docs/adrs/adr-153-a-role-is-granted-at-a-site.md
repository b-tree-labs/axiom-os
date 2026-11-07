# ADR-153: A role is granted at a site, and authority composes two ways

**Status:** Accepted (2026-10-02)
**Builds on:** [ADR-151](adr-151-roles-separate-governing-from-reading.md) (the role vocabulary),
[ADR-045](adr-045-raci-evolution.md) (D6, the autonomy ladder),
[ADR-050](adr-050-facility-is-a-domain-term-platform-uses-tenant-site.md) (tenant and site vocabulary),
[ADR-052](adr-052-database-tenancy-schema-per-extension.md) (one install, many tenants)

## Context

ADR-151 gives a role a meaning. It does not say **where** that meaning
applies, and the registry it lands in is keyed by role alone. So a role
means the same thing everywhere on a node.

That is wrong for the deployment we actually have. One node hosts several
sites, and they are different organisations with different appetites: what
one publishes, who one trusts to administer, and how much it will let run
unattended are its own decisions, not a node-wide constant. A site needs
its own world.

And yet people cross. Institutional IT administers every site on a node.
An operator covers two. A credential-hygiene agent sweeps all of them. If
the only grant available is per-site, every one of those people collects a
pile of near-identical bindings and the node has no way to say "this person
administers everything".

Both are true at once, which is the thing to design for rather than choose
between.

A second problem arrives with the first, and it is easy to miss because it
looks like the same problem. Some settings resolved against a principal's
grants are not permissions at all: they say **how much may happen without a
person watching** — the ADR-045 D6 tier, and anything that resolves to one.
Compose those the way permissions compose and a single node-wide grant
quietly raises the autonomy of every site that had deliberately chosen less.

## Decision

Four of the six sections below are the same rule seen from different
angles, and it is worth naming once: **a site's own statement about itself
is never overridden by a wider grant.** It is never raised in autonomy
(§3), it may withdraw a binding it did not ask for (§4), it cannot have one
imposed from inside itself (§5), and it publishes its own floor (§6).

### 1. A grant names where it applies

A role binding is a triple: **principal, role, where**.

```
(@sam:one-site, operator, one-site)  runs one site
(@it-staff, admin,    *)       administers every site on this node
(@keep,     operator, ut)      an agent, bound where it sweeps
```

`where` is a site id, or `*` for the node. `*` includes sites created
later, which is the point of it: an administrator of everything should not
silently stop administering when somebody adds a site. An enumerated list
is accepted too, for a deployment that wants each site named.

A wildcard is not a claim that nobody minds. It is the **hosting
relationship** written down — the party that invited a site administers it
by default — and §4 is what keeps that honest.

A principal has a home site. It does not confine them; it is where a
binding goes when nobody says otherwise.

### 2. Scopes compose by union

The effective scopes for a request are the union of what the principal
holds **at that request's site** and what they hold at `*`.

This is what the triple exists for. Cross-site IT staff hold their
authority once and it reaches everywhere, and a site administrator's grant
stops at their own site because nothing widens it.

### 3. Autonomy composes by minimum

Any value resolved against a principal's bindings that expresses **how much
may happen unsupervised** — an ADR-045 D6 tier, or a setting that resolves
to one — takes the **minimum** across the bindings that declare it, not the
union.

A principal at `act-then-notify` on the node and at `prompt-every-time` at
one site acts at `prompt-every-time` when acting at that site.

The failure this prevents: a site sets itself conservative, somebody grants
a node-level binding for an unrelated and good reason, and that site is
raised without anybody deciding to raise it. Isolation would be decorative
exactly where it matters most, and nothing on screen would say so.

A site that has declared **nothing** inherits, because silence is not a
risk position. A site that has declared is never raised by a wider binding.

### 4. A site may withdraw a wider binding

A site that was invited onto a node starts with the inviting party's
binding **in force**, and may **withdraw** it at any time. Withdrawal is
the invited site's own decision, taken by its owner, effective on the next
request, and recorded.

So a `*` binding reaches a site unless that site has withdrawn it. The
invitation says this in words the invitee reads before accepting, because a
default that is only discovered later is not a default anybody agreed to.

**What withdrawal is, and is not.** It is honoured by the gate and written
into the audit record: it governs what somebody is entitled to do and what
the record says they were entitled to when they did it. It is **not** a
physical boundary. Whoever operates the node can reach its storage whatever
this says, and a partner agreement that implies otherwise is promising
something this design cannot deliver. Say the smaller true thing.

**Withdrawal is a transfer of duty, so it is refused until the site can
carry it.** Cutting off the inviting party's administration means the site
now owns administrator succession, continuity and recovery for itself.
Those are real obligations and they arrive silently, so two things are
required rather than warned about:

- **The site holds at least two of its own administrators.** One is a
  single point of failure, and the ordinary way a site orphans itself is
  that the one person leaves. A site with fewer cannot withdraw; the
  refusal says why and names what to do about it.
- **The withdrawal records an acknowledgement**, naming the site, the
  person, the date and what the site is taking on. It is a decision with
  consequences and the record should read like one, which also means
  nobody has to reconstruct later who accepted them.

**Break-glass, because a site can still orphan itself.** Two
administrators can both leave. A node owner may restore a withdrawn
binding; the restoration is recorded and the site is told, and the site is
told at withdrawal time that this exception exists. An unannounced
restoration would make withdrawal meaningless, and an undisclosed one would
make the original acceptance uninformed.

### 5. Only a node-level grant creates a node-level grant

A principal whose binding is at a site may not create, widen or move a
binding to `*`, whatever role they hold there. Without this, a site
administrator grants themselves the node and the boundary is decoration.

### 6. The floor is per site

`guest` names published surfaces (ADR-151 §3), and each site publishes its
own. The public face of one site is not the public face of another, and a
surface published at one does not appear at the next.

## Consequences

**Every grant gains a field, including the ones that exist.** A binding
with no `where` has to mean something during migration, and it means the
principal's home site rather than `*` — the narrower reading, which may
remove access somebody currently enjoys. That is the correct direction for
the mistake to run, and it has to be announced rather than discovered.

**Two composition rules in one resolver is a thing people get wrong.** The
union is the obvious one and will be applied to everything by anybody who
has not read §3. The resolver should therefore refuse to compose a value it
cannot classify, rather than defaulting to union, so a new setting of this
kind fails loudly at the seam instead of silently granting more autonomy
than any site agreed to.

**An agent binding is a real binding.** A sweep that runs across sites
holds `(agent, role, *)` and is subject to both rules: it reaches every
site, and at each one it acts at that site's tier. This is what makes a
cross-site agent safe to grant.

**Invitations carry two things now**, a role and a where, so the invitation
record and every surface that issues one grows a field. A site invitation
carries a third: the binding the inviting party will hold, stated plainly
enough that accepting it is a decision rather than a formality.

**Withdrawal needs somewhere to live and somebody to press it.** A site
cannot withdraw anything until it has an owner of its own, so establishing
one is part of onboarding a site rather than a later nicety. Until then the
inviting party's binding is the only administration that site has.

**Federation is untouched.** ADR-151 §4 stands: a node is authoritative for
its own principals and a peer's grants are claims it may refuse. Sites
within a node are not peers, and nothing here widens what a peer can say.
