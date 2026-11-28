# ADR-284: A site keeps its decisions in one settings file, with a section per extension

**Status:** Accepted (2026-11-24, approved by the owner)
**Related:** [prd-settings](../prds/prd-settings.md) (layers ST-2, safety keys ST-3, extension sections ST-5), ADR-209 (the site's declared configuration is the `managed` layer), ADR-254 D6 (a node-level file read before any database is up, overrides in a table), ADR-065 (extension config schema), ADR-142 rule 8 (administration holds no logbook authority), ADR-174 (Postgres is the runtime database), `docs/specs/spec-settings.md`; first user: attest gate-role pairs (`attest.gate_roles`, #1800) and the logbook unit O1

## Context

Settings has five layers (`managed`, `node`, `project`, `user`, `session`, prd-settings ST-2) and no persistence rule for the two that belong to a site. A site's decisions live in about ten files, one per subsystem (`site-auth.toml`, `connectors.toml`, `telemetry.toml`, and so on), each with its own loader. A site manager cannot see them in one place, and no change to them goes through one audited path. The settings store (`axi settings`, `settings.toml`) already has a dotted-key store, a web and MCP face, and a change log, but only for per-user and per-project preferences. Attest reads site values through `site_settings.value`, and nothing on `main` registers a source for it, so a site cannot set `attest.gate_roles` at all.

## Decision

**One mechanism, in the platform, for every extension.** It lives in `axiom.infra` and not in the settings extension's screens. An extension declares a section, a schema and its authority keys in its manifest (ADR-065) and reads values through one call. It has no file, loader or database code of its own. `site_settings.value` becomes a thin reader over it.

### The model

1. **The `node` layer is a ledger.** Settings changes are entries in an append-only ledger in Postgres (a schema owned by the platform, ADR-052, ADR-174). An entry is `{seq, section, key, old, new, actor, time, key_revision, prev_hash, hash, signature}`. The `node` layer's value of a key is the fold of its entries. The `managed` layer is the site repository's declared `settings.toml` (ADR-209), installed by the deploy, read-only on the node and stronger than `node`: a key it sets is locked (ST-4) and changes only by a change to the repository. A key the repository leaves unset is changed through the ledger. The deploy never writes to the ledger.
2. **The local file is a signed projection, not a second source.** After each committed entry the node renders the effective settings into `settings.toml` (atomic write) with the ledger head (`seq`, `hash`) and a signature by the node key. It is what the node reads when the database is unreachable, and it is what backups hold. Keys needed to reach the database (its address, the node key) are in the `managed` file only (the pattern of ADR-254 D6) and never in the ledger.
3. **One write path, compare-and-swap.** Every writer (web, MCP, CLI, an extension) calls the same function with the `key_revision` it read. A stale revision is refused, so two managers, or a manager and a file edit, can never silently overwrite each other. The entry is committed first, then the file is rendered. If rendering fails, the ledger is still right and the reconciler re-renders.

### Reconciling the two

4. **The file and the ledger are compared at every start and on a timer.** The file's `seq` and `hash` against the ledger's chain:
   - Equal: nothing to do.
   - The ledger is ahead and extends the file's head: re-render the file.
   - The file is ahead and extends the ledger's head (a restored or lagging database): replay the missing entries into the ledger, after verifying every hash and signature.
   - Neither contains the other (a fork, or a rollback to an older copy): **freeze.** Every authority key reads as unset, nothing in the ledger or the file is changed, and the owner is told what diverged. A person chooses which side wins, and that choice is itself an entry.
5. **A hand edit of the file is a proposal, not a change.** The file's signature fails, so it is not applied. The reconciler shows it as a proposed entry from "file edit" and applies it only if the key is not an authority key and the section's schema accepts it. An authority key edited by hand is ignored and reported.

### Adversarial updates

6. **Authority keys need a signature.** A section marks a key `authority = true`. An entry for it must carry the owner's signature, checked on read against the owner's key, not only on write. A row inserted by SQL, or a line added to the file, has no valid signature and is ignored and reported. Site-admin can read these keys and cannot sign a change to them (ADR-142 rule 8).
7. **Authority keys are set at the `node` layer only.** The store refuses them at `project`, `user` and `session`, and ignores and reports one found in those files.
8. **A section is written by its owner.** The writer's identity comes from the caller's context, never from the payload. Writing into another extension's section is refused. Two extensions claiming one section name fail at discovery, naming both.
9. **Values are checked.** Every entry is validated against its section's schema, with size and type limits and no executable content, before it is committed and again when read.
10. **Rollback and replay are detected.** The head `seq` and `hash` are recorded in three places: the file, the ledger, and an anchor the node publishes offsite on a timer (the attest anchors already do this for logbooks). A restore to an older state is a fork under item 4.
11. **A damaged file or ledger never loosens anything.** Damage, a failed signature or a frozen state reads authority keys as unset and the safety dials at their restrictive values. The cause is shown to the site manager with its fix.

### Composing and overriding

Layers are the settings PRD's (ST-2): `managed`, `node`, `project`, `user`, `session`, strongest first; below them sit the owner's schema default, an extension's default and the product's default. An extension override (C3) is a layer between the defaults and `managed`, never above it. A safety key still resolves to its most restrictive value across layers (ST-3); C2 extends that rule to extensions.

C1. **Closed by default.** A key cannot be overridden unless its owner's schema says so: `overridable_by` names who may, and `direction` is `narrow` or `any`.
C2. **Narrowing is automatic for a signed extension; widening needs the owner.** A key declares what more restrictive means: a numeric bound, an enum order or a subset. A signed extension's override that narrows is accepted without anyone's approval. One that widens needs a signed approval from the site owner, recorded in the ledger.
C3. **An override is declared, never written.** The extension lists `[[settings.overrides]]` (key, value, reason) in its manifest. The platform checks it against the parent's rules and schema. An extension never writes the ledger for a key it does not own.
C4. **An override is a layer, not an edit.** The parent's value is unchanged. The effective value is computed with its provenance (extension, version, manifest hash), and `axi settings explain <key>` shows the chain. Removing the extension removes its override and leaves nothing to clean up.
C5. **Scoped by default.** An override is seen only by the extension that declared it. It is global only where the parent's schema grants that.
C6. **Order does not matter.** Narrowing is commutative, so two extensions narrowing one key give the same result in any load order: the most restrictive value wins. Widening is not commutative, so only a signed owner approval can do it.
C7. **A changed extension is checked again.** A new manifest hash revalidates its overrides. A widening that was never approved falls back to the parent's value, and the owner is told.
C8. **An unsigned or local extension needs the owner's approval first,** even to narrow, because its manifest has no verified provenance.
C9. **Authority keys are never overridden by an extension.** They change only by a signed site-ledger entry from the owner. An extension may propose a value, such as a gate pair, and only the owner can sign it.

### Node type and federation

N1. **Settings stay on the node.** No peer, in the organization or outside it, reads or writes a node's settings through federation. Several nodes of one site share values only through the `managed` layer, which each node installs from the site repository. Letting an accord expose any value to a peer would need its own ADR.
N2. **A key declares where it applies.** `applies_to` names the node roles and functions a key means something for. A key that does not apply to this node is shown as not applicable, with the reason, and cannot be set there.

### Backends, classification, evolution, actors and authority

B1. **Two ledger backends, one entry format.** A node with a database keeps the ledger in Postgres. A node without one (a collector, which holds no data platform and accepts no inbound connection) keeps it as a hash-chained file, written with the same locked, atomic append the action chain uses. Entries, hashes and signatures are identical, so a chain moves between backends when a node gains or loses a database. A node that can only push out sends its head to a host node on a timer, which is the anchor of item 10.
B2. **A protected value is never stored in the ledger.** A key carries a `sensitivity` label from the platform's access labels. A value above the site's threshold, such as a hostname or an address a site protects, lives in the vault and the setting holds a reference to it (the rule of ST-9). The ledger, the file, the offsite replica and the site repository then hold references only, and where a replica may live is a question about the vault, not about every key.
B3. **The settings schema evolves by expand, then contract (ADR-182).** A release may add a key and may accept an old name beside a new one (`renamed_from`). Removing a key or changing its type happens in a later release, marked `# contract: <what> unused since <version>`, and CI refuses it otherwise, as the migration guard does. A ledger entry for a key this release does not know is kept, shown as unknown to this release, and never dropped or rewritten, so two releases can read one ledger during a blue/green switch.
B4. **An actor is a person, an agent, or a granted admin agent.** A key says which actor classes may write it (`writable_by`); for an agent the default is none, and an agent may always read and propose. An admin agent that manages deployments, infrastructure and customer data is the exception, and it gets it by grant, not by exemption: a named grant lists the sections and keys it may change; it works by plan, then approval, then apply, with the plan's hash approved by a person on a surface the agent cannot operate; each entry records the agent and the accountable person; a rate limit applies. Authority keys are never writable by an agent. The plan, hash, approve and apply pattern is the one every admin action in Axiom uses, not only settings.
B5. **A change to an authority key is a record in a logbook.** A gate pair is a signed entry in a site-authority log, with a co-signature where the log's rules require one, so Axiom has one signing system and the change sits in the controlled record. The settings ledger entry for the key carries a reference to that record (its id and hash), and a reader verifies it through a seam. Settings core stays in `axiom.infra` and does not import the extension: the signed-record extension registers the verifier, as `site_settings` does today.

B6. **Approval of an agent's plan is a person's fresh tap over the plan's hash.** The surface alone does not keep an agent out: an agent with a shell can run the CLI, and one with browser control can click in a person's signed-in page. What it cannot do is hold a person's second factor. So an approval is a signature by the accountable person, made with a fresh MFA tap (the KEEP `require_mfa` boundary; the session-persistent cache in `axiom.infra.stepup` is not enough), over the plan's hash. The approver is shown the plan rendered by the platform from the plan itself, never text the agent supplied. An approval is single-use, expires after a short time, names the approver, and is recorded with the plan in the signed record. It arrives as a "needs you" notification, so it can be answered on a second device.
B7. **Three tiers of change by an admin agent.** Within its grant and narrowing a value: no per-change approval, rate-limited and recorded. Outside its grant, or widening: a person's approval under B6. An authority key, or anything irreversible: the owner's approval under B6 and, where the log's rules say so, a second person's co-signature. The same tiers are meant to apply to every admin action, not only settings.

### What this does not defend against

An attacker who holds both root on the node and the node key and the owner's key can forge a valid chain. The offsite anchor (item 10) turns that into a detectable fork and not a silent change; it does not prevent it.

### Operating it

12. **A deploy changes `managed` and nothing else.** A `node` entry stays until someone removes it, and removing it falls back to the layer below. A key set in `managed` cannot be changed remotely; a site that wants a value managed from the web leaves it out of the repository.
13. **Everything is recoverable.** The ledger is in Postgres, so offsite replication covers it. The baseline is in git. The file is in backups. Any one of the three can rebuild the others by item 4.
14. **The existing site files keep working.** Each moves into this mechanism one at a time, the old file read as a fallback, and no one is made to change or sign in again.

## Consequences

- A site manager sees and changes a node's decisions in one place, from the web, MCP or CLI, with no SSH.
- A removed gate pair stops conferring a signing role at the person's next sign-in. Roles in `roles.toml` are untouched.
- Attest's `site_settings` source reads this mechanism, so O1's pairing and O2's `ops_log.*` settings share it.
- It is more to build than a file store: a ledger table, the render and reconcile steps, signed entries and the offsite anchor. It is built in chunks, tests first, starting with the model's smallest slice (items 1, 3 and 8).
- The action chain (`actions.jsonl`) remains the record of who invoked what. It is on the node's disk and keeps a digest of parameters, so it is not the recovery source for values; the ledger is.
