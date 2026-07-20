# ADR-045: RACI evolution — dimensional matrix, sensitive-path multiplier, learned overrides, earned graduation

**Status:** Accepted (2026-05-26)
**Date:** 2026-05-01 (D1–D5); 2026-05-26 (D6 amendment)
**Decision Makers:** Benjamin Booth, Team
**Supersedes:** none (extends `infra/raci.py` v1, `feedback_raci_hil`, `feedback_raci_automation_escalation`)
**Related:** ADR-026 (ownership model), ADR-035 (human principal binding), ADR-046 (RIVET/TIDY boundary — first consumer of D6), `infra/raci.py`, `policy/agent_action_guard.py`, `prd-rev-u-pr-review.md`, `prd-agents.md`

---

## Context

`infra/raci.py` v1 ships a 5-level global trust slider that maps each
action category to one of `A` (approve) / `C` (consulted) / `I`
(informed). The trust matrix hardcodes two safety invariants:

```python
"code.patch":   ["A", "A", "A", "A", "A"],   # always approve
"code.commit":  ["A", "A", "A", "A", "A"],   # always approve
```

Both `code.patch` and `code.commit` are forced to `A` at every trust
level — including L5 ("Full Trust"). This was the right defensive
default for v1, but field experience and the REV-U PRD design pass
have surfaced six structural problems:

1. **Reversibility collapse.** A docstring fix on a dirty branch
   (one `git checkout`) carries the same approval weight as a
   force-push to a release tag (effectively irrecoverable). The
   slider can't distinguish them.
2. **Blast-radius collapse.** Editing a working-tree file, committing
   to a feature branch, pushing to a protected branch, publishing to
   PyPI, and broadcasting via federation are all "code.commit" today.
   The risk profiles differ by orders of magnitude.
3. **Domain-criticality collapse.** Edits to `tests/`, `docs/`,
   `auth/`, and `infra/raci.py` carry identical weight. Sensitive
   paths warrant a tighter gate than the rest of the tree.
4. **Slider-meaning collapse at the top.** L5 is meant to encode
   "Full Trust except safety." For code work, L5 behaves identically
   to L1 — every patch is gated. The top end of the slider is
   indistinguishable from the bottom for the most common action.
5. **Composition friction with new agents.** REV-U's value depends on
   being able to *propose* fixes (post a suggestion, apply a
   pre-cleared lint autofix, retract a comment). The blunt rule
   gates each of those, making the differentiator agent annoying.
   RIVET is in the same boat for routine version bumps (previously BURN-E; folded into RIVET 2026-06-01).
6. **No earned trust.** L5 is user-declared, not user-earned. Nothing
   observes whether the user has *demonstrated* the judgment that
   L5 implies. Granting maximal autonomy by self-attestation is the
   wrong default for an agent platform that holds principal-bound
   keys per ADR-035.

The current rule is the right safety floor but the wrong ceiling. We
need to keep the floor and lift the ceiling without flattening the
risk gradient.

---

## Decision

RACI v2 composes **three layers** to determine whether an action
requires human approval. Each layer has well-defined inputs, outputs,
and overrides. Trust ceases to be a single global slider and becomes
a **per-action trajectory** earned through observed behavior.

### D1 — Dimensional base matrix

Replace the 1-D trust matrix with a 2-D matrix of
(`action_kind` × `blast_radius`). Each cell defaults to A/C/I per
trust level. The trust level is now per-action, not global.

**Action kinds** (extensible; current minimum set):

| Kind | Examples |
|------|----------|
| `code.edit` | modify a working-tree file |
| `code.commit` | record to git history |
| `code.push` | propagate to a remote |
| `code.tag` | annotate / sign a release tag |
| `code.publish` | release to a registry (PyPI, npm) |
| `code.federation` | broadcast attested artifact via `axiom://` |
| `code.review.finding` | post a review comment |
| `code.review.retract` | withdraw a posted finding |
| `code.review.suggest_patch` | post a diff suggestion |
| `code.review.apply_patch` | land a patch as a commit |

**Blast radius** (the modifier):

| Radius | Reversibility | Visibility |
|--------|---------------|------------|
| `local` | one `git checkout` | none |
| `feature_branch` | one `git reset` / branch delete | repo-local |
| `protected_branch` | revert commit (history kept) | team-wide |
| `tag` | rare to delete; convention says don't | team + downstream |
| `release_artifact` | effectively irreversible | public |
| `federation_broadcast` | signed; revocation is a separate ceremony | cohort-wide |

**Default A/C/I per trust level** (sample cells; full table in code):

| Action × Radius | L1 | L2 | L3 | L4 | L5 |
|-----------------|----|----|----|----|----|
| `code.edit.local` | A | A | C | I | I |
| `code.commit.feature_branch` | A | A | C | I | I |
| `code.push.feature_branch` | A | A | A | C | I |
| `code.push.protected_branch` | A | A | A | A | A |
| `code.tag.release_artifact` | A | A | A | A | A |
| `code.publish.release_artifact` | A | A | A | A | A |
| `code.federation.federation_broadcast` | A | A | A | A | A |

**Safety floor** is encoded *structurally* by always-A at the
high-blast-radius end of the matrix. Everything below the floor scales
with trust.

### D2 — Sensitivity multiplier

The multiplier fires when ANY of four signal sources matches the
in-flight action. On match, bump the cell **one tier toward A**
(`I → C`, `C → A`; `A` stays `A`). Classified and export-controlled
matches additionally promote certain cells to the always-A floor
(see D5).

**D2.1 — Path glob match.** A configurable allowlist marks
intrinsically sensitive paths. Per-repo override in
`.axiom/raci.toml`:

```toml
[raci.sensitive_paths]
patterns = [
  "auth/**",
  "crypto/**",
  "policy/**",
  "infra/raci.py",
  "infra/identity/**",
  "**/*secret*",
  "**/*credential*",
  ".github/workflows/**",   # CI changes alter the safety net itself
  "pyproject.toml",         # dep changes have supply-chain reach
  "uv.lock",
]
```

**D2.2 — Classification label match.** When the touched file or the
in-flight session carries a classification marking, the multiplier
fires regardless of path. Sources, in priority order:

1. **In-band marking** — file content begins with a recognized
   classification banner (`# CLASSIFIED`, `# CONFIDENTIAL`, etc.)
   or carries a `[classification]` frontmatter block.
2. **Sidecar metadata** — `<file>.classification.toml` next to the
   file (or under `.axiom/classification/<path>`) with a labeled
   level.
3. **Path-level inheritance** — `.axiom/classification.toml` declares
   directory-level labels (`classified/**`, `ec/**`, etc.) with
   inheritance.
4. **MemoryFragment provenance** — when the file was created from a
   classified fragment, the classification carries forward via
   ADR-027 federation metadata.

```toml
[raci.classification]
levels = ["public", "internal", "confidential", "classified", "ec"]
multiplier_floor = "internal"   # public stays unmultiplied; everything ≥ internal multiplies
```

**D2.3 — Active routing tier.** When
`agent._session_mode in {"export_controlled", "classified"}` (the
existing `--mode` flag from `axi chat` and `axi signal`), the
multiplier fires for the entire session. Reason: even if the touched
file isn't itself marked, *any* action taken in an EC session
context is a potential carrier of EC content. The session's
classification dominates.

**D2.4 — Cohort attestation.** When the artifact is governed by a
federation cohort that declares it classified or EC (per ADR-022
classification spec, ADR-027 federated memory), the cohort's
attestation flows in as a multiplier signal. Mismatched-classification
operations (e.g., committing an EC artifact to a non-EC remote)
are caught here.

**Examples (composing all four signals):**

| Scenario | Multiplier signal | Effect at L5 |
|----------|-------------------|--------------|
| Trust-5 user edits `docs/api.md` | none | `I` (no prompt) |
| Trust-5 user edits `auth/login.py` | D2.1 path glob | `I → C` (consulted prompt with explanation) |
| Trust-5 user edits `infra/raci.py` | D2.1 path glob (meta-rule) | always-A regardless |
| Trust-5 user edits a file marked `# CLASSIFIED` in-band | D2.2 in-band marking | `I → C` plus classification floor (D5) may force `A` for some radii |
| Trust-5 user runs `axi chat --mode export-controlled` and edits any file | D2.3 routing tier | `I → C` for the whole session |
| Trust-5 user attempts to commit EC-marked artifact to public remote | D2.4 cohort mismatch | always-A and likely refused (D5 floor) |

### D3 — Learned overrides (memorized exemptions and uplifts)

Every approval prompt is also a learning event. Each override is
persisted as a `MemoryFragment` with structured scope:

```python
@dataclass
class RaciOverride:
    scope: OverrideScope    # principal + (action, path_pattern, args_shape)
    direction: str          # "exempt" (skip prompt) | "uplift" (force prompt)
    weight: float           # 0..1, based on frequency + recency
    source: str             # "user_explicit" | "system_proposed" | "cohort_inherited"
    expires_at: datetime | None
    superseded_by: FragmentID | None
```

Three ways an override is created:

1. **User explicit** — `axi raci remember <action> <path-glob> --exempt`
   or via the chat approval prompt's `[A]lways for path-pattern X`
   choice. Highest weight; never expires unless revoked.
2. **System proposed** — after N consecutive approvals of the same
   `(action, path_pattern)` shape (default N=10), the system asks:
   `"You've approved 10/10 of <pattern>. Skip prompt for this
   pattern in this session?"`. User confirms → exempt persisted at
   medium weight. User declines → counter resets. Three declines
   for the same shape → stop proposing per
   `feedback_raci_automation_escalation`.
3. **Cohort inherited** — at federation cohort registration, the
   user can opt to inherit a peer's overrides (cohort-attested).
   Inherited overrides carry `source = "cohort_inherited"` and the
   peer's signature; they bypass the prompt only when the local
   user has reached the matching trust level.

Overrides are queried *after* D1 + D2 produce the base RACI. An
exempt override turns `A` or `C` into `I` for that pattern. An
uplift override does the reverse. Overrides are visible via
`axi raci show` and revocable via `axi raci forget <pattern>`.

### D4 — Earned graduation

Trust level is no longer a user-set slider for the whole user. It's
a **per-action ladder** that the system promotes the user up when
observed behavior justifies it.

Per-action graduation drivers:

- **Approval rate** — fraction of prompts approved at the current
  level over the last N=50 invocations.
- **Retraction rate** — fraction of approvals later regretted (e.g.,
  reverted commits, dismissed REV-U findings the user later asked
  for).
- **Time in level** — minimum dwell time per level (default 7 days
  per action) prevents same-day L1→L5 jumps.
- **Severity exposure** — graduation through high-severity actions
  (anything touching a sensitive path) requires double the dwell.
- **Cohort vouching** — federation peers at higher levels with
  signed attestation can lower the bar by ½.

When the system computes `current_level + 1` is justified, it
proposes:

> "You've approved 47 of your last 50 `code.commit.feature_branch`
> prompts with no retractions. Graduate this action from L3 → L4
> (Autonomous)? L4 means we'll inform-only on this action and leave
> approval to you for everything else. Confirm with `/raci graduate`,
> defer with `/raci later`, or decline with `/raci no`."

Three declines → stop proposing; user can still graduate manually.

Demotion is automatic on retraction-rate spikes (> 20% in 10
actions). The user is informed; demotion is not blocking.

### D5 — Always-A floor (preserved structurally)

These cells are *always* `A`, regardless of trust level, sensitive
paths, classification context, or learned overrides. The floor is
not user-overridable.

**D5.1 — Unconditional floor (any context):**

- `code.publish.release_artifact` — irreversible, public
- `code.federation.federation_broadcast` — signed cohort attestation
- `code.tag.release_artifact` — convention says don't delete tags
- `code.push.protected_branch` — branch protection has its own
  semantics; the protected list is from `git config` + repo-policy
- Any action touching `infra/raci.py` or `infra/identity/**`
  (meta-rule to prevent self-modifying the rule out of existence)

**D5.2 — Classification-conditional floor.** When D2.2 / D2.3 / D2.4
fires (the action is touching classified or EC content, OR the
session is in `export_controlled` / `classified` routing tier, OR
the cohort declares the artifact controlled), additional cells
collapse to always-A. Some collapse all the way to **refuse**
(operation is rejected, not gated):

| Action × radius | Classification context | Floor behavior |
|-----------------|------------------------|----------------|
| `code.federation.federation_broadcast` | EC content | **refuse** — separate ceremony required |
| `code.publish.release_artifact` | classified content | **refuse** |
| `code.push.protected_branch` to a non-classified remote | classified content | **refuse** (cohort-mismatch) |
| `code.commit.feature_branch` | EC content into a non-EC branch | always-A, with classification carry warning |
| `code.edit.local` | classified content while session mode is `public` | always-A, with mode-mismatch warning |
| Any `code.review.*` | classified content visible to a reviewer not cleared at the cohort's level | **refuse** |

**Refusal is not a missing approval.** The operation is rejected with
an actionable explanation: the user hasn't been blocked from doing
something legitimate by an over-cautious gate; the operation isn't
legitimate in the current context. The error names the next
ceremony (declassify request, cleared-cohort federation push, mode
switch) and points at the relevant ADR.

The classification floor preserves a hard separation between the
trust-aware part of the matrix (where graduation reasonably operates)
and the policy-enforced part (where graduation has no business
operating). Trust does not graduate over classified material; that
gate is structural, attested, and audited per ADR-022's
classification invariants.

The floor is the only true safety invariant. Everything else is
trust-aware.

### D6 — Graduation safety (added 2026-05-26)

D4 graduates an action from `C` (prompt every time) to `I` (never
prompt). That binary is the wrong granularity: earned autonomy is then
either nagging or blind. The 2026-05-26 GitLab-mirror CI flood is the
canonical pitfall — a single unattended action firing hourly on a
schedule, flooding the operator's inbox. D6 adds the missing middle rung
and the brakes, reusing primitives already present in
`policy/agent_action_guard.py` (`AgentAction.reversible`, the
per-invocation volume bound, and the planned "approval-on-first" +
"cooldown after action class").

**D6.1 — The `act-then-notify` tier (`N`).** Insert a tier between `C`
and `I`. At `N` the agent performs the action, then surfaces it in a
**batched digest with a one-click undo window** (default 24h). The
ladder becomes `A → C → N → I`. `N` — not `I` — is the default
destination of earned graduation for reversible actions; fully-silent
`I` is reserved for trivially reversible, high-frequency, low-blast
actions (e.g. `code.edit.local`).

**D6.2 — Reversibility-gated graduation.** An action graduates to `N`
or `I` only if `AgentAction.reversible` is true **and** a concrete undo
exists (archived ref, `git stash branch`, revert commit). Irreversible
classes — the D5 floor, plus any force / non-ancestor delete, publish,
tag, or federation broadcast — never graduate past `C`, regardless of
earned trust. This binds D4 to D5 through reversibility: **trust
graduates over the undoable; it never graduates over the irreversible.**

**D6.3 — Volume / rate circuit-breaker.** Generalize the static
per-invocation volume bound (`AGENT_ACTION_DEFAULT_MAX_PER_TICK`) into a
learned baseline per `(action, op_class, scope)`. If a graduated action
would act on ≫ K× its rolling-median batch size, or fire more than R
times per window, the breaker trips: demote that burst to `C`, alert the
operator, record the anomaly. Rationale: the flood failure mode is
**volume/rate, not per-action correctness** — a class can be individually
safe and collectively harmful. The breaker is the structural defense the
CI flood would have needed.

**D6.4 — Novelty confirmation (scope envelope).** Implements the guard's
planned "approval-on-first." Graduation is scoped to an observed
envelope (the repos, branch-name shapes, path patterns seen during the
trust trajectory). An action targeting a scope outside the envelope
re-prompts once (`C`) even if the class is graduated, then extends the
envelope on approval. Prevents silent generalization of earned trust to
unseen contexts.

**D6.5 — Digest batching (no per-event noise).** Autonomous (`N`/`I`)
actions never emit per-event notifications; they roll into the owning
agent's periodic briefing to AXI. The operator sees "TIDY reclaimed 9
merged branches (undo: `axi hygiene undo <id>`)", not nine pings.
Preventing a notification flood while preventing an operational one is
explicit: **D6 must not trade one flood for another.**

**D6.6 — Per-tier behavior.**

| Tier | Prompt? | Acts | Notify | Undo | Earned for |
|------|---------|------|--------|------|------------|
| `A` | yes, blocking | after approval | — | n/a | floor + low trust |
| `C` | yes, blocking | after approval | — | n/a | mid trust / sensitive |
| `N` | no | immediately | batched digest | undo window (≈24h) | earned **and** reversible |
| `I` | no | immediately | digest only | n/a | earned + trivially reversible + high-freq + low-blast |

The circuit-breaker (D6.3) and novelty confirmation (D6.4) can
transiently bounce any `N`/`I` action back to `C` for a specific burst
or scope **without** demoting the earned level.

**D6.7 — First consumer.** ADR-046 (RIVET/TIDY boundary) is the first
consumer. TIDY's merged-branch / remote-ref prune runs at tier `N`:
archive-ref-before-delete supplies reversibility (D6.2), `git branch -r
--merged` + RIVET's `rivet.pr_merged` signal supply confirmation, the
circuit-breaker (D6.3) guards against a mass-delete burst, and
reclamations batch into TIDY's digest (D6.5). RIVET — which only ships
and signals — exposes no graduated destructive action.

The floor (D5) remains the safety invariant; D6 governs how the
trust-aware region *acts* once graduated, never how the floor behaves.

---

## Consequences

**Positive**

- Trust 5 finally means something for code work. `code.edit.local`
  and `code.commit.feature_branch` can flow without prompts at L5,
  the way users intuitively expect.
- REV-U becomes pleasant at trust 4-5: lint autofixes, typo
  corrections, and retractions land without friction. Sensitive
  paths still gate. Differentiator agent isn't strangled by
  protocol.
- Earned graduation matches the security-clearance metaphor: trust
  is observed, not declared. New users can't self-attest to L5.
- Learned overrides give users muscle memory: "Always approve this
  exact pattern" finally means it across sessions.
- Federation cohorts can share trust judgments; lab admins
  effectively delegate by signing a baseline override set once.
- The safety floor (publish, federation, tag, protected-branch
  push, raci-self-edit) remains structurally always-A — not subject
  to user override at any level.
- Classification and EC content are first-class signals, not
  retrofitted as path globs. Carry-over via in-band markings,
  sidecar metadata, path inheritance, and federation attestation
  composes cleanly. Trust never graduates over classified material;
  that gate is policy-enforced, not trust-aware.
- Misclassification operations (commit EC to public branch, broadcast
  classified via federation) are *refused with a path forward*, not
  silently approved or vaguely warned. The error names the next
  ceremony.

**Negative**

- Migration is non-trivial. Every existing `check_raci()` call site
  needs to pass an action-kind + blast-radius, not just an action
  string. Roughly 20–40 call sites estimated; each is mechanical.
- Per-action graduation state must be persisted per-principal — new
  storage in `~/.config/axi/raci-state.json` (per-principal) +
  optional federation sync.
- The 2-D matrix can be hard to internalize for new users. We
  mitigate with `axi raci explain <action>` showing the cell + its
  current overrides + the path that's about to be touched.
- Cohort-inherited overrides introduce a subtle trust-graph
  dependency. Mitigated by always showing the source on prompts:
  "(inherited from @lab-admin:utne via signed attestation
  2026-04-15)".

**Migration plan (post-Prague)**

1. Land RACI v2 behind a feature flag (`RACI_V2_ENABLED`); v1
   behavior preserved by default. Write the per-action defaults +
   sensitive-path config into `infra/raci/v2/`.
2. Migrate call sites to the new signature one extension at a time
   (chat first, then RIVET, then REV-U which lands directly on v2).
3. Add `axi raci show / explain / remember / forget / graduate` CLI
   surface.
4. Land the propose-then-confirm graduation engine as a hook
   subscriber to `tool.post_invoke`.
4a. Land D6 graduation-safety in `policy/agent_action_guard.py`: the
   `act-then-notify` tier + undo window, the reversibility gate (reuse
   `AgentAction.reversible`), the learned-baseline volume/rate
   circuit-breaker (generalize `AGENT_ACTION_DEFAULT_MAX_PER_TICK`),
   novelty/envelope confirmation, and digest batching. ADR-046's TIDY
   prune is the first consumer and lands incrementally on this tier.
5. Federation-attested override sync ships in V2.1.
6. Flip the flag default after one minor version of soak time.

REV-U's PRD references this ADR. REV-U V1 (P1+P2) ships under v1
RACI's blunt rule and inherits v2 benefits when the migration lands.
The PRD's "Open question 4" (LLM tier per pass) is independent and
not affected.

---

## Tracking

- Implementation lives at `src/axiom/infra/raci/` (package, not
  single file) once V2 lands.
- Per-principal state at `~/.config/axi/raci-state.json` plus
  cohort-shared fragments in the federated memory store.
- Test coverage target: 95% on the dimensional matrix lookup, the
  sensitive-path multiplier, the override engine, the graduation
  engine, and the safety-floor invariant. The floor invariant gets
  property-based tests — *no input combination, including malicious
  ones, should produce non-A for floor cells*.
- Telemetry: emit `raci.decision` events on every check with
  (action, radius, sensitive_match, override_hit, level, decision)
  for evaluating the graduation thresholds in production.

---

**2026-06-01 — BURN-E retired.** Role (heartbeat-liveness watch-the-watcher) is now `release/heartbeat_liveness_audit` on RIVET. No remaining src/ code; runtime dir at `~/.axi/agents/burn-e/` may be archived locally.
