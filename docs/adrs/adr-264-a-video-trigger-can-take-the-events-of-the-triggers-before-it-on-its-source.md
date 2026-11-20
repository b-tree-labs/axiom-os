# ADR-264: A video trigger can take the events of the triggers before it on its source

**Status:** Draft
**Related:** ADR-240 D3 (a trigger is a registered contract that emits events), ADR-256 D7 (an event assumes no frame), ADR-220 §3 (a reload that does not build changes nothing), ADR-238 D2 (a trigger never names a recipient), [spec-video](../specs/spec-video.md) §`StreamTrigger` and §Trigger runner, [prd-video](../prds/prd-video.md) Phase 5

## Context

A consumer's processing is rarely one step. The first one wants to read a scene
in stages, each cheap enough to run only when the one before it says something
happened: motion, then which regions of a fixed view are visible, then what each
visible region holds. Every stage is a trigger, and every stage is worth keeping
separately: its own version, its own feed in the source's journal, its own health,
replaceable on its own.

ADR-240 D3 makes `observe(frame)` the only thing a trigger is given. The triggers
on one source are siblings on the frame bus and none can see what another wrote.
A consumer today either puts every stage inside one trigger, or reads another
trigger's feed back out of the journal.

## Decision

**An attachment can name, with `on`, the attachments on the same source whose
events it takes, and the runner gives it those events alongside its frames.**

- `on = ["<trigger>", "<trigger>:<kind>"]` in the attachment's table: another
  attachment on this source, every event or one kind (`started`, `ended`,
  `occurred`). `on` is the runner's, like the attachment's `name`: the factory is
  never given it.
- The trigger takes events by implementing `on_event(event) -> TriggerEvent*`
  (`TakesEvents`, beside `StreamTrigger`). The runner calls it on the trigger's own
  thread, never at the same time as `observe`, so a trigger never needs a lock.
- It is given each event its upstream wrote to the journal, after the write, in
  the order written, the `ended` the runner writes for an episode it closes
  included. Events are merged with its frames in frame time: an event comes before
  the first frame at or after its own time.
- What `on_event` returns is checked and journaled like anything `observe`
  returns, so a trigger can be on a trigger that is on another: a chain.
- An `on` that cannot work is refused when the attachments are checked, with its
  fix: an attachment that is not on this source, itself, a cycle, a kind that is
  not one, or a trigger that does not take events. A reload with one changes
  nothing (ADR-220 §3).
- Pending events are bounded per trigger: one that falls behind drops its oldest
  and counts them, and nothing upstream waits, as with frames.

This extends ADR-240 D3 and leaves the rest of it as it was. An event still assumes
no frame (ADR-256 D7): a trigger is given the event, not the frame the upstream
saw. On a fixed view a region is the same pixels in every frame, so a stage that
needs pixels reads its own next frame.

## Options considered

- **Every stage in one trigger.** No platform change. Rejected: the stages lose
  their own version, feed and health, and replacing one replaces all of them.
- **Chain through the journal**: a stage reads the feeds it follows from the
  source's journal with a cursor of its own. Works across processes. Rejected for
  now: a cursor, a poll and a delay for what is one process, and a stage still
  needs frames from the bus. It stays open for a stage that must run elsewhere,
  a GPU host for example.
- **One event bus every trigger hears.** No configuration. Rejected: every trigger
  pays for every event, and nothing in the connector file says which trigger
  depends on which, so a reload cannot refuse a broken dependency.
- **The runner gates frames** ("feed this trigger only while that one has an
  episode open"). Rejected: too narrow. A stage wants the frame after an episode
  ends and the scene settles, or a reading on a change elsewhere, and with the
  events in hand a trigger can gate itself.
- **Hand the upstream's frame with its event.** Rejected by ADR-256 D7, and a
  frame held for every pending event is a 4K buffer each.

## Consequences

- A consumer builds a pipeline out of small triggers, attached and replaced by
  configuration like any other.
- The connector file shows every dependency, and `status` shows what a trigger is
  on and how many events it was given and dropped.
- A trigger is given events written while it runs. Nothing replays history to a
  trigger that starts later; one that needs it reads the journal.
- Chains stay within one source and one process. Across sources, or on another
  host, is the journal-cursor option above, if a consumer needs it.
- Follow-ups: spec-video (`TakesEvents`, `on`, the runner), prd-video Phase 5
  (VID-027..030).
