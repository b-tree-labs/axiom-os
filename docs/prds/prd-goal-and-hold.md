# PRD — A goal, and a hold the helper cannot pass

**Product / Feature:** Goal and hold

**Owner:** Ben Booth   •   **Status:** Active   •   **Last updated:** 2026-10-19

**Spec:** `docs/specs/spec-aeos-1.0.md` §20.12
**ADRs:** ADR-139 (a caller's purpose is not permission; amendment 2026-10-19), ADR-140 (which local model answers)

---

## 1) Elevator pitch

A person states what done is and which decision only they may make. The helper re-reads that and stops. It does not invent the next step, and it does not send a command at a subject nobody named.

## 2) Problem

A helper with one conversation will continue a checklist past a hold, and will aim a command at whatever subject is nearby in the transcript. That is the failure. A second system that guesses the subject, keeps a tree of guesses, and promotes the guess does not fix it, and it is not in this PRD.

## 3) What ships

1. **`/goal` writes one record.** What done means, which decisions only a named person may make, and the subject (a unit, a tail, a line). Opening or replacing it is an action. It does not raise a rung and it is not certification. Spec: §20.12.
2. **`/loop` re-reads that record.** One increment per wake. No goal, no loop. When the next increment is a reserved decision, the loop stops and asks. Stopping is not a demotion.
3. **The prompt for a turn is that goal plus the recent turns of that goal.** Other goals stay out of the prompt. Switching goals is something the person does.
4. **A command with no named subject is not released.** A model may draft. The draft does not actuate. The envelope and the interlock do not graduate.

## 4) Done when

- A checklist whose next step is reserved to a person produces a stop, not the step.
- A request that names a subject only by a description ("the one by the door") produces no command.
- A second, unrelated question does not change the open goal's prompt.
- No new store, classifier, Postrule seat, or MCP intent surface is added for this.

## 5) Explicitly not in this work

Sensed intent, an intent tree, revision edges, compaction of guesses, retrieval recipes chosen by a guess, and exposing intent as an MCP primitive. ADR-139 already forbids using a caller's prose as the reason an action was permitted. This PRD does not reopen that.

## 6) Model

Whichever provider `runtime/config/llm-providers.toml` names, under ADR-140. This PRD does not add a router.
