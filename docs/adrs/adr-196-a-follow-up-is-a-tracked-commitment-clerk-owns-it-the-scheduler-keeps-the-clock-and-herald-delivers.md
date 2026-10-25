# ADR-196: A follow-up is a tracked commitment: CLERK owns it, the scheduler keeps the clock, and HERALD delivers

**Status:** Accepted (2026-10-20)
**Related:** [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md) (the composed CLERK: skills are commons; CLERK owns the clock, the composition and the accountability), [ADR-165](adr-165-program-self-update-and-per-consumer-change-detection.md) (the append-only journal beside the data), [ADR-166](adr-166-program-membership-is-a-principal-not-a-tracker-seat.md) (a person is a principal), [ADR-167](adr-167-the-program-is-editable-through-its-own-tool.md) (the program is editable through its own tool), [ADR-162](adr-162-the-watcher-primitive.md) (the watcher primitive), [ADR-182](adr-182-axiom-never-goes-down-from-its-own-causes.md) (a silent stop must be attributable), [ADR-185](adr-185-tools-declare-what-they-need-and-make-and-vetted-chains-are-published-as-recipes.md) (tools declare what they need and make), [ADR-052](adr-052-database-tenancy-schema-per-extension.md) (a schema per extension), [ADR-114](adr-114-mcp-authority-enforcement.md) (the transport identity gate), [ADR-174](adr-174-postgres-is-the-runtime-database-and-sqlite-is-for-tests.md) (Postgres at runtime), `docs/specs/spec-axiom-schedule.md` (PULSE), `docs/prds/prd-program.md` (R26), `docs/specs/spec-program.md` ("Follow-ups"), `docs/guides/follow-ups.md`

## Context

After a risky step is approved but not finished, someone has to come back to it.
A table swap leaves the old table in place for a day; a deploy has a rollback
window; a key rotation has a period in which both keys work. The honest plan for
each is "check on it tomorrow, then drop the old one". Today that sentence is
remembered by the chat session that heard it. The session ends, the context is
compacted, the person switches harness, and the commitment is gone. Nothing
failed; nothing was ever recorded anywhere that outlives the conversation.

The question put to the platform was "Clerk or the scheduler?". Four facts in the
code decide it.

- **The scheduler already keeps a durable clock, and it is not a tracker.** PULSE
  (`extensions/builtins/schedule`) supports a one-shot cadence with a due time,
  records each fire in `schedule_fire_log` under a unique claim
  `(schedule, bucket, params hash)` so one instant fires once, and
  `reconcile_pending` recovers a fire whose owner process was killed. It has no
  notion of an owner, an acknowledgement, a snooze, a check, or evidence. It runs
  an action string, which is a qualified skill name. A row that has fired stays
  `active` with a null next time; it never says "done".
- **A non-reentrant one-shot is lost to a `kill -9` mid-fire.** `reconcile_pending`
  marks the fire `interrupted`, and the next fire time of a one-shot after a fire
  is none. A reminder that vanishes when its host crashes at the wrong moment is
  the failure this feature exists to prevent. The fix is local to us: our action
  is idempotent, so we register it `reentrant=True`, and a sweep re-arms any open
  follow-up that has lost its row.
- **The update-approval ladder (HERALD) is the right shape, and the wrong
  container.** `update/escalation.py` has `Rung(day, channel, audience, kind)`,
  an injected clock, a `step` that runs each rung once, and `answer` that stops
  the ladder. But its state is one JSON file per pending update with update words
  baked into the message. We reuse the `Rung` shape and HERALD's delivery; we do
  not reuse the file or the wording.
- **CLERK is already the owner of "the record that it ran".** ADR-161 gives CLERK
  the clock, the composition and the accountability, and says why: a cadence with
  no attributable owner decays, and a silent stop looks like a quiet week. A
  follow-up is exactly a tracked commitment with an owner, a due time and
  evidence of done, which is the program item shape.

## Decision

**CLERK owns the commitment. The schedule substrate (PULSE) supplies the clock.
HERALD delivers and escalates.** Each does one thing and none of them re-implements
another.

### D1. CLERK owns the follow-up

A follow-up is a tracked program commitment, stored in the program extension's
own schema (`program`, ADR-052, Postgres per ADR-174), with:

- an `id`, a plain-language `what`, and an `owner` (a principal, `@name:context`);
- a `due_at`, and `created_by` (the requester principal, attributed: the
  principal that asked, not the agent that typed it);
- an optional `check`: a registered, read-only skill plus arguments, run at due
  time so the reminder carries the current state instead of nagging blindly;
- a `state`: `open`, `due`, `acknowledged`, `snoozed`, `done`, `expired`;
- an `ladder` of rungs (D3), and `evidence` links;
- an append-only **journal** of everything that happened to it
  (`followup_event`): created, armed, check ran, delivered (per rung, per
  channel, and whether the recipient was reachable), acknowledged, snoozed,
  re-armed, escalated, closed, expired. CLERK's journal says "this fired", so a
  silent stop is visible: a follow-up past its due time with no `delivered` event
  is reported as **missed** in every list, never as quiet.

The skills (`program.followup_add`, `_list`, `_show`, `_snooze`, `_done`, `_ack`,
`_owed`) are commons (ADR-161): any principal in any harness reaches them through
the composed MCP (`axiom_program__followup_*`) and the CLI (`axi program
followup ...`). They do not belong to the agent.

### D2. The scheduler supplies the clock; we add no daemon

A follow-up's next action time is registered on PULSE as a one-shot cadence whose
action is the skill `program.followup_fire`, carrying only the follow-up id in
the envelope. The orchestrator service that already hosts PULSE binds the program
skills into its registry and runs a cheap `program.followup_sweep` cadence. PULSE
provides the durable due-time job, the exactly-once claim, and restart recovery.
Specifically:

- every follow-up action row is `reentrant=True` and `misfire_policy="fire_once"`
  (overdue after downtime fires once on restart);
- the fire skill is idempotent: it records an `attempt` journal row first and a
  `delivered` row after, and sends with a dedup key `followup:<id>:<rung>`, so a
  fire killed between the send and the record is re-run on restart and HERALD
  suppresses the duplicate. The effect is exactly one delivery per rung;
- `program.followup_sweep` (interval cadence, registered at orchestrator start)
  re-arms any open follow-up whose PULSE row is missing or cancelled, expires
  follow-ups past their horizon, and is the attributable heartbeat of the
  feature.

The CLERK daemon agent's own 15-minute heartbeat (`program sync`) is not used as
the clock: it is opt-in, autonomy ships off, and a reminder must not depend on an
operator having armed a data-file reconciler. The clock is the orchestrator.

### D3. HERALD delivers and escalates; a principal with no channel is surfaced

On fire CLERK (1) runs the CHECK, (2) composes the reminder (what, the check's
output, the exact next command), (3) delivers it to the owner through HERALD's
`send`. If the owner is not acknowledged, the ladder continues. The ladder reuses
HERALD's `Rung` shape (offset from due, channel, audience, kind). The default:

| After due | Audience | Kind |
|---|---|---|
| 0 | owner | reminder |
| 6 h | owner | reminder (second channel as the recipient's profile orders it) |
| 24 h | requester | flag ("the owner has not acknowledged") |
| 48 h | operator | contact the owner |

An acknowledgement stops the ladder; `done` closes it; `snooze` moves the due time
and resets the ladder. Past the last rung with no answer the follow-up becomes
`expired` and stays in "what do I owe" so it is still seen. **Delivery to a
principal that has no reachable channel is surfaced, not dropped:** HERALD falls
back to the inbox and the receipt says whether that is durable; if the receipt is
not durable, or the owner has no profile and no inbox, the journal records
`unreachable`, the next rung's audience is escalated early to the requester and
the operator, and `program status` lists it.

Quiet when nothing is due: the sweep that finds nothing writes nothing, and a
`done` or `acknowledged` follow-up produces no message.

### D4. The check is bounded, read-only and allowlisted

The CHECK is a skill name plus arguments. At creation, the skill must exist, must
declare `risk = "read"` and `side_effects = False`; otherwise the add is refused
with the fix. At fire time it runs headless with a time bound and an output byte
bound, through the registry (so authz, audit and ADR-157 budgets apply). Its
output is reduced to an allowlist: only the declared result fields that are
scalars or short strings are kept, and the whole message passes the secret scan
(`axiom.infra.secret_scan`) with any finding replaced by a typed marker and a
journal note. **A secret value never appears in a follow-up, its check output or
its message.** A follow-up stores the *name* of a credential in no field; if one is
needed the person says "the rotation key" and the check reads it from the vault.

### D5. Visibility is per principal

A follow-up is visible to its owner, its creator, and principals it is shared with
(`shared_with`, an explicit list). `list` and `owed` answer for the *calling*
principal, never for a principal named in a parameter unless the caller is that
principal. Over MCP the calling principal is the transport identity the ADR-114
gate admitted (`AXIOM_MCP_CLIENT_PRINCIPAL`); the skill context's own `principal`
is the node owner on that path, so the skills read the gate's identity rather than
the context's. Creating a follow-up owned by someone else is a maintainer or
deputy action; anyone else gets a refusal that says so. One principal's follow-ups are
invisible to another's list unless shared.

### D6. Training: when the agent creates one itself

CLERK's persona, and each skill's `next_steps`/`requires` declarations (ADR-185),
teach one rule: after a risky or irreversible step is **approved but deferred**
(a swap with an old table to drop, a deploy with a rollback window, a key rotation
with an overlap period) the agent **proposes** a follow-up and asks for consent in
the same turn. It does not create one silently. The proposal names the owner, the
due time, and the check it would run. Creation is non-destructive and reversible
(`done` closes it), so the tier is propose then ask then back off (ADR-161): if the
person declines twice for a given kind of step, CLERK stops proposing that kind.
`axi program status`, and the composed MCP status, list the calling principal's due
and open follow-ups first, because AGENTS.md tells agents to consult the tracker
for current priorities.

## Options considered

- **The scheduler alone.** A cron row is a clock, not a commitment. It cannot say
  who owes the work, whether anyone has seen it, what the state was when it fired,
  or that it did fire. A fired one-shot looks the same as one that never fired.
  Rejected.
- **CLERK alone, on its own heartbeat.** Duplicates the clock the platform already
  has, ties a reminder to an opt-in agent that ships disabled, and a 15-minute
  granularity is wrong for "in two hours". Rejected.
- **HERALD alone (a delayed notification).** HERALD delivers; it has no state for
  "acknowledged", "snoozed" or "done", and a message with no owner is the status
  quo's reminder with extra steps. Rejected.
- **A host-specific reminder (the harness's own scheduled task).** Works for one
  harness, in one machine's session store, and not from the others. The point is
  that Axiom, not a chat, remembers. Rejected.
- **CLERK commits, PULSE ticks, HERALD tells (chosen).** Smallest new surface:
  one schema, a handful of skills, and no new daemon.

## Consequences

- A follow-up created from Claude Code, Codex, ChatGPT or the CLI lands in the
  same table and fires from the same orchestrator. There is no harness-specific
  path.
- The orchestrator now needs the program skills bound in its registry. A node that
  runs no orchestrator holds follow-ups that never fire; the "missed" marker and
  the doctor-visible sweep heartbeat make that visible rather than quiet.
- `reentrant=True` plus the dedup key trades a possible repeated *attempt* after a
  crash for exactly one *delivery*. We choose that over a lost reminder.
- Escalation reaches the operator only after the owner and the requester have had
  their rung, and never leaves the node's HERALD channels.
- The PRD gains R26; R25 ("absence is a follow-up, not a judgment") is the private
  program-head agenda derived from the data file, and is a different thing: R26
  follow-ups are explicit commitments a principal made.
