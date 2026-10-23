# ADR-155: An agent may contain a leak before it may manage a key

**Status:** Proposed (2026-10-02)
**Builds on:** [ADR-153](adr-153-a-role-binding-names-where.md) (where a binding applies), [ADR-151](adr-151-roles-separate-governing-from-reading.md)
(scope vocabulary and role bundles),
[ADR-045](adr-045-raci-evolution.md) §D6 (the `A → C → N → I` ladder)
**Relates to:** [ADR-114](adr-114-mcp-identity-gate.md) (the MCP identity gate)

## Context

A live LLM API key was dumped into an agent transcript by a `pytest`
traceback that rendered `os.environ`. The platform noticed nothing. The
first thing that looked at the problem was a human reading the transcript.

Four separate things had to be false for that, and all four were:

1. **The capability is not on the agent surface.** The `secrets` extension
   sets `[extension.mcp] enabled = false`. An agent gets three read-only
   vault tools — `audit`, `reconcile`, `resolve` — and cannot record an
   exposure or rotate anything. The verb that exists for exactly this
   event, `axi secrets exposed`, is CLI-only.
2. **The watcher is not switched on.** The `vault` agent (KEEP) ships on a
   3600s heartbeat and is `(not enabled)`; consent covers `hygiene` and
   `release`. It had not run in ten days.
3. **It would not have mattered.** KEEP's charter is custody and capability
   lending over credentials already in the store. Nothing's job is to find
   credential material that was never in custody. `secrets.discover`
   answers exactly that and is scheduled by nobody.
4. **The scanner could not see the key anyway.** Fixed separately: no
   matcher recognised a hyphen-segmented vendor key, the process-environment
   probe was Linux-only on a macOS fleet, and nothing read a shell startup
   file.

(4) was a bug and is fixed. (1)–(3) are policy, and policy is the subject
here, because the honest reason nothing acted is that **nothing was
permitted to**.

## Decision

### 1. Authority is a named preset over scope × tier

A single on/off for "may an agent touch secrets" is the wrong shape: the
verbs differ by an order of magnitude in blast radius. But a third
vocabulary would be worse. The levels are a **composition of two things
ADR-151 and ADR-045 already define** — a scope granted to the agent's
principal, and the RACI tier for that action class:

```
secrets.agent_authority = observe | propose | contain | manage
```

| level | scope | tier | meaning |
|---|---|---|---|
| `observe` | `secrets:read` | — | audit, discover, resolve, list, metadata get. **Fallback when nothing is declared — see §6.** |
| `propose` | `secrets:read` | `C` | records an exposure, queues a rotation; a human approves |
| `contain` | `secrets:invoke` | `N` | executes `exposed` — record **and** rotate — then notifies, 24h undo |
| `manage` | `secrets:invoke` | `N` | scheduled `rotate`; D6.3's volume breaker bounds a wide sweep |

Nothing new is enforced. The authz hook already checks the scope; the
agent ladder already decides autonomy. The setting is the operator-facing
**name for a pair**, which is what makes it configurable in one step.

### 2. Notation: colon for scopes, dot for settings

`secrets:invoke` is a scope. `secrets.agent_authority` is a setting. The
separator is load-bearing and the two namespaces must never be read for
each other.

### 3. `set` and `rm` are a floor, not a default

They are off the agent surface at **every** level, including `manage`, and
no role may grant them. `set` writes a value the agent chose, which is a
credential-substitution primitive rather than a hygiene action; `rm`
destroys the only copy. Neither is a tier — there is no autonomy setting
at which "the agent picked the new secret" is the intended behaviour.

### 4. A resource-level deny always wins over a role-level allow

Credential metadata carries `agent_rotation = allow | deny`. The
composition order is one-directional: **deny beats allow, never the
reverse, and no role may widen past it.** A role says what a principal may
do; it does not say that this particular credential is exempt.

This is for credentials whose consumer set is not fully known — an
audit-chain HMAC, a database password read by services nobody has
inventoried — where the automatic rotation is the dangerous act even
though the credential is the kind an agent could otherwise handle.
`secrets.discover` reports consumers as well as unmanaged material, and
that index is **incomplete by construction**; `deny` is how an operator
says so.

### 5. The decision point is GUARD

KEEP's persona already states that it "enforces custody, not policy" and
delegates permit/deny to GUARD. `secrets.agent_authority` resolves to a
GUARD input. It does not become a second gate inside the `secrets`
extension, and no path consults one without the other.

### 6. Scopes union across sites. Autonomy takes the minimum.

[ADR-153](adr-153-a-role-binding-names-where.md) makes a role binding a
triple — principal, role, and **where**, a site or `*` for the node — and
a request's effective scopes the **union** of what the principal holds at
that site and what they hold at `*`. A site administrator is
`(them, admin, site-b)`; cross-site IT staff are `(them, admin, *)`.

Union is right for a scope and wrong for a tier, and this setting is both.

A scope answers *may this principal do the thing at all*, and a person who
administers every site should hold the union of what that means. A tier
answers *how much of it may happen unsupervised here*, and a site that has
chosen `observe` has made a statement about its own risk that a node-level
binding must not quietly overrule. Unioned, a single `(agent, admin, *)`
grant at `contain` would upgrade every partner site on the node — the
isolation would be decorative in exactly the way ADR-153's own rule about
node-level grants is written to prevent.

So resolution is split by what is being resolved:

- **Scope** — `secrets:read` / `secrets:invoke` — is the union across the
  principal's bindings at this site and at `*`, per ADR-153.
- **Tier** — which of the four levels applies — is the **minimum** over
  the same bindings. A principal at `contain` on the node and `observe` at
  site-b acts at `observe` when acting at site-b.

**`observe` is a fallback, not a stored value.** ADR-153 §3 distinguishes
a site that has DECLARED a tier from one that has said nothing: silence
inherits the node's, and a declaration is never raised by a wider
binding. Writing `observe` into every site at creation would defeat that
— every site would read as declared, and a node-level setting would never
reach anywhere, with no way for the operator to see why. So the order is:

1. the site's own declaration, if it has made one — never raised;
2. otherwise the node's (`*`) declaration, inherited;
3. otherwise `observe`, because nothing anywhere has said.

Two rules carried from ADR-153 unchanged: only a node-level grant may
create a node-level grant, and the floor is per site, so each site
publishes its own surface.

This is what makes the scheduled sweep safe to grant broadly. A sweep
principal bound `(agent, role, *)` reaches every site — including sites
created later — and acts **at each site's own tier**: reporting
everywhere, containing only where a site has agreed to it.

Per ADR-153's consequence for the next person: a resolver that meets a
value of this kind it cannot classify must refuse to compose it rather
than fall back to union. Union is the obvious rule and will be applied by
anyone who has not read §3; failing loudly at the seam is cheaper than
quietly granting more autonomy than any site agreed to.

### 7. Discovery is scheduled, and reports at every level

The sweep that finds unmanaged credential material runs on a cadence at
**every** level including `observe`, because finding and reporting mutate
nothing. Only what may happen next changes with the level. A deployment
that never raises the level above the default still gets the answer to
"what am I not managing?" — the question that went unasked here.

## Consequences

**A fresh install still does nothing on its own.** Nothing has declared
a tier, so the fallback applies and an unconfigured node reports and
never acts. That is right for
a partner site, and it means this ADR changes nobody's blast radius until
an admin decides it should.

**Prompt injection is bounded by the ladder, not eliminated.** An agent
reading a hostile repository or log is the standing risk, and secrets
mutation is the highest-value target for it. At `observe` and `propose`
the worst case is noise and a queue of bad proposals. At `contain` it is a
denial-of-service by rotation — recoverable, visible, inside the 24h undo
window, and the reason §3 is a floor. A site that cannot accept that stays
at `propose`.

**`[extension.mcp] enabled = false` must change above `observe`.** The
read-only tools cannot implement `propose`, which writes an exposure
record. That is a real widening of the agent surface and should land with
the GUARD enforcement, not before it.

**The queue at `propose` is a new human surface.** A proposal nobody reads
is worse than none, because it reads as coverage. It routes to HERALD,
which already owns alerting humans about credential events.

**The tier is not the only thing bounding the agent, and on macOS it is
the only thing.** ADR-036 D10 confines a platform-managed agent on
systemd — `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`,
writes confined to the state dir, and it runs as a user unit rather than
root, which is what keeps the process probe to that user's own processes.
On macOS there is no confinement at all: `ProcessType=Background` is a
scheduling class. So on the platform this fleet develops on, every level
above `observe` is bounded by this ADR's policy and by nothing else.

That is survivable because of what the sweep carries rather than where it
runs: a finding is a locator, a matcher and a truncated SHA-256, and no
value leaves the scan. But it means `contain` on a macOS workstation is a
policy promise, not an enforced one, and a site weighing `contain` should
know which it is getting.

ADR-036 D10 now carries a `network = false` axis for exactly this shape of
agent — a scanner's risk is egress, and `ProtectHome=read-only` is a grant
to read. The sweep cannot use it yet: it runs inside KEEP, whose
`outbound_call` needs the network. Splitting the scanning half into its
own network-free unit is the change that would make `contain` enforced
rather than promised, and it is not in this ADR.

**Rejected: a single boolean.** It forces the most dangerous verb and the
safest onto one switch, so the setting is either useless or unacceptable
and every deployment picks "off".

**Rejected: a third vocabulary.** An earlier draft defined the four levels
as primitives in their own right. They are not: they are presets over
ADR-151 scopes and ADR-045 tiers, and saying so is what keeps there from
being two systems to reconcile later.

**Rejected: inferring authority from the credential alone.** Attractive —
the credential knows its own blast radius — but it leaves no way for a
site to be more conservative than its credentials' metadata, and partner
sites are exactly where that is needed. §4 keeps the credential's voice as
a veto, not as the grant.
