# How to ask Axiom to remind you, and what happens next

**Status (2026-10-21):** on main since PR 1353 (ADR-196); **not in Axiom 0.70.1**, the
latest release on PyPI, so `axi program followup` answers `invalid choice` until the next
release. Verified on main: `axi program followup --help` lists `add, list, owed, show,
snooze, ack, done`. Not verified: the create, due and delivery steps, which need the node's
data-platform database with its migrations applied. On an install without that database the
command prints a database traceback (`relation "followup" does not exist`) instead of a plain
"the follow-up store is not set up" message; run these commands on a node, not on a fresh laptop.

The colleague steps for installing and connecting a harness are in the consumer layer's guide
(each product built on Axiom ships its own colleague quickstart).

## Before you start

- A node with the data-platform database and its orchestrator running (see "How you know it did not silently stop").
- Your harness is connected to the node's MCP server. Claude Code and Codex use the local server; ChatGPT needs a remote MCP address, which a campus-private node does not expose, so it cannot call these tools there today.

A guide for anyone who has just done something risky and wants Axiom, not a chat
session, to remember to come back to it. The decision behind it is
[ADR-196](../adrs/adr-196-a-follow-up-is-a-tracked-commitment-clerk-owns-it-the-scheduler-keeps-the-clock-and-herald-delivers.md)
(CLERK owns the commitment, the scheduler keeps the clock, HERALD delivers); the
contract is "Follow-ups" in `docs/specs/spec-program.md`; the requirement is R26 in
`docs/prds/prd-program.md`.

The idea in one line: **a follow-up is an owner, a due time, an optional read-only
check and a place to say it is done, kept by Axiom itself, so it is still there
tomorrow whichever harness you are in.**

## Ask for one

### From Claude Code, Codex or any harness on the composed MCP

Say it in plain words. The agent proposes it and asks first; say yes.

> Remind me in 24 hours to approve the drop-old, and show me the verify result.

> The swap is done. Don't let me forget to drop the old table in two days, and check
> the row counts when you remind me.

The agent calls `axiom_program__followup_add`. What it sets is shown to you before it
does: who owns it, when it is due, which check it will run, and the exact command you
will be given. If you did not ask for a follow-up, an agent that has just finished a
risky step (a swap, a deploy with a rollback window, a key rotation) should still
offer one. It should not create one without your yes.

To see what you owe, ask: *what do I owe?* (`axiom_program__followup_owed`). Starting
a session by reading the program status (`axiom_program__status`) shows the same,
first.

### From the CLI

```bash
axi program followup add \
  --what "drop the old orders table" --due 24h \
  --check data.table_status --check-args '{"table": "orders_old"}' \
  --next-command "axi data drop-old orders_old"

axi program followup owed          # what do I owe: missed and due first
axi program followup list --state due,open
axi program followup show --id fu-1a2b3c4d      # with the journal
axi program followup snooze --id fu-1a2b3c4d --by 24h
axi program followup ack --id fu-1a2b3c4d       # I have seen it; stop reminding me
axi program followup done --id fu-1a2b3c4d --note "dropped; counts matched"
```

`--due` takes `30m`, `24h`, `2d`, `1w`, or an ISO-8601 timestamp (a timestamp with no
zone is read as UTC; the result echoes the time it resolved to).

### From chat

Chat uses the same skills. Say the same sentence you would say to a harness; chat
shows you the proposal and asks.

## What happens at the due time

1. The scheduler fires the follow-up. If the check was named, it runs: read-only, with
   a 20 second limit, as you (the person who asked).
2. You get one message through HERALD, on the channels your recipient profile lists
   (the inbox is always the floor): what is owed, the check's current state, the exact
   next command, and how to acknowledge, snooze or close it.
3. If you say nothing, it climbs. Defaults: a second reminder at +6 hours, a flag to
   whoever asked at +24 hours, a note to the operator at +48 hours. The operator is
   `AXI_FOLLOWUP_OPERATOR` if set, else the program deputy. A rung that would tell
   someone who has already been told is skipped.
4. `ack` stops the reminders (it stays on your list until `done`). `snooze` moves the
   due time and starts again from there. `done` closes it with a note and evidence.
5. If you cannot be reached on any channel, the person who asked and the operator are
   told at once, not 24 hours later.
6. Past the seven-day horizon with no answer it becomes `expired`. It stays on your
   list so it is still seen.

When nothing is due, nothing is sent.

## How you know it did not silently stop

Each follow-up has a journal (`followup show`): created, armed, check ran, delivered,
acknowledged, snoozed. A follow-up that came due and was never delivered is marked
**missed** on every list, and `owed` puts missed first. The usual cause is that no
orchestrator is running on the node: reminders fire from the data-platform
orchestrator service (`python -m axiom.extensions.builtins.data_platform.orchestration`,
or the node's service for it), which also keeps the heartbeat that re-arms any
follow-up that lost its clock row. There is no separate daemon.

## What stays private

- A follow-up is visible to its owner, the person who created it, and anyone you list
  with `shared_with`. Another principal's `list`, `owed` and `show` do not see it.
- Over MCP the caller is the identity the server admitted, never a parameter.
- Creating a follow-up owned by someone else needs the program deputy or a maintainer.
- A follow-up never holds a secret. A request that contains a credential-format value
  is refused (the refusal does not repeat it); check arguments with names like
  `token` or `api_key` are refused; a check's output is reduced to short fields and
  scrubbed before it reaches a message or the journal. Name a credential ("the
  rotation key"), do not paste it.

## For agent authors

The skill descriptions and the CLERK persona carry the rule: propose after a deferred
risky step, ask, create on a yes, back off after two declines of the same kind. The
`capabilities` hints (`needs followup-id`, `makes followup_id`) come from the skills'
declarations (ADR-185), so nothing here is hand-kept.

## You are done when

- `axi program followup owed` lists the follow-up you asked for, with its due time.
- Asking *what do I owe?* in your harness returns the same list.
- After the due time you received one message on your recipient channels, and `followup show` has a `delivered` entry in its journal.
