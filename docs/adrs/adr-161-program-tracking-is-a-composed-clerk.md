# ADR-161: Program tracking is a composed extension with a thin CLERK coordinator

**Status:** Accepted (2026-10-05)
**Related:** [ADR-060](adr-060-event-routing.md) (event routing), ADR-156 (oversight surfaces, in flight), [ADR-162](adr-162-the-watcher-primitive.md) (watcher primitive), `docs/prds/prd-program.md`, `docs/working/clerk-program-steward-design-2026-10-05.md` (narrative record)

## Context

The platform's first consumer program needed its tracker, a published status
site, and a wiki kept current without anyone writing status reports. The first
Monday cycle (2026-10-05) was run entirely by hand: evidence gathered from
repos and the journal, progress comments drafted and posted, priorities
proposed per owner, a site rendered, assignees reconciled. The question was
what shape automates this: new skills alone, one new full agent, or a
coordinator over the agents that already exist (rivet, press, herald, keep,
tidy). The same week's delegation-spine work (#1067) made agent-to-agent
delegation real, with namespace-filtered toolsets, a depth cap, and requester
attribution.

## Decision

Program tracking is a domain-agnostic `program` extension whose skills are
commons, coordinated by one deliberately thin agent persona named **CLERK**
(the court clerk: keeps the docket and the record, never judges).

- The skills (status, collect, draft, post, priorities, render, attach) are
  invocable by any principal and any harness through the composed MCP and the
  CLI; they do not belong to the agent.
- CLERK owns only the clock, the composition, and the accountability: the
  scheduled cycle, the assembly of collect→draft→priorities→render→post, and
  the journaled record that the sync ran. Specialist steps are DELEGATED over
  the spine: rivet (git and CI evidence), press (publishing), herald (digests
  and asks), keep (credential rotation), tidy (staleness evidence).
- The governing principle is evidence over reports: nobody reports status; the
  clerk summarizes recorded work and never invents progress. Writes are
  consent-tiered (propose, then ask, then back off) and requester-attributed.

## Options considered

- **Skills alone, no agent.** A cadence without an owner decays; no
  attributable actor's journal says "the sync ran," so a silent stop looks
  like a quiet week.
- **One fat program agent.** Re-implements rivet, press, and herald inside a
  new body; breaks the one-job-per-agent pattern; widens one sandbox instead
  of composing narrow ones.
- **Coordinator over existing agents (chosen).** Smallest new surface; the
  spine's confused-deputy controls apply as built.

## Consequences

- Program tracking scales by configuration, not code: one human with two
  builder agents and a ten-person program run the identical extension, and
  programs compose (a child clerk's published summary is just another
  evidence source to a parent).
- The clerk becomes the first consumer of the watcher primitive (ADR-162) and
  of the oversight-surfaces journal; its activity lands in the Log like every
  other emitter.
- Follow-up: `prd-program.md` carries the requirements; the tech spec lands
  with the implementation; a consumer deployment contributes only
  connector configuration (tracker ids, credentials, roster, publish targets).
