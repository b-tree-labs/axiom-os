# ADR-260: A site's feature flags live on its site node, and a console coordinates sites

**Status:** Accepted (2026-11-12). Decided by the owner.
**Related:** ADR-254 (one registry), ADR-257 (the loop), ADR-037 (signed state propagation), ADR-016 (multi-node federation), ADR-164 (node functions).
**PRD:** [The loop](../prds/prd-the-loop.md), LOOP-F1 to F32.

## Context

The cohort and rollout work (LOOP-F1 to F23) keeps each node's flags, cohorts, rollouts,
stage plans and kill switches in that node's own database. That is right for one node and
wrong for a fleet: rolling a feature out to every site in a time zone would mean repeating
the same rollout on every node, with nothing keeping the copies in step and no single
answer to "what is rolled out where".

Two facts shape the answer. A site is several machines with roles (collectors, relays, a
site node, and development nodes bound to the site), and the machines already lean on
their site node. And managing rollouts across many deployed sites is a console's job, done
by an administrator of the fleet, not by a person using one site's app; the owner has
decided the console comes later and the command line and MCP serve until then.

## Decision

**A site's flag record (flags, cohorts, rollouts, stage plans, schedules and kill
switches) lives on its site node. The site's other machines receive it from the site node.
A console, when it exists, coordinates many sites by sending each a signed change through
federation; it is not the place every node reads from.**

- **D1. One record per site.** The site node holds the authoritative record. A write on any
  other machine of the site is refused with the site node named.
- **D2. Machines receive, they do not author.** Collectors, relays and bound nodes receive
  the site's record over the binding they already have, verify it, and resolve locally.
  Resolution stays on the machine (one evaluation, LOOP-F12), so a machine keeps serving
  when its link to the site node is down.
- **D3. Offline behaviour.** A machine keeps the last record it received and verified, and
  reports its age. A machine that has never received one resolves from the registry
  defaults and says so. Nothing is guessed.
- **D4. The kill switch travels first.** A kill switch change is sent ahead of any other
  change, and a machine applies it on receipt. A machine that cannot be reached is
  reported as not yet stopped, never assumed stopped.
- **D5. The console coordinates sites.** A fleet change (for example "turn on for every
  site in one time zone") is a set of per-site changes, each signed and sent to the site's
  node through federation (ADR-016, ADR-037), each previewed and approved under that
  site's policy. A site that declines or is unreachable is shown as such; the console
  never writes into a site's record directly.
- **D6. Until the console exists,** the command line and MCP on a site node manage that
  site, and the same changes can be scripted across sites by an administrator holding
  each site's grant.

## Options considered

- **One record for the whole fleet, read directly by every node.** Simpler to reason
  about, but every site would depend on one central service to resolve a flag, and some
  site nodes are on private networks that cannot reach it.
- **One record per node, with a tool that repeats each change on every node.** Nothing new
  to build, but copies drift and there is no authoritative answer.

## Consequences

- The records built for LOOP-F1 to F23 stay where they are on a site node; on any other
  machine of a site they become a received copy. The work owed: refuse writes off the site
  node, the receive-and-verify path over the binding, the record's age in explain, and the
  kill-switch-first ordering.
- A node's "emergency stop" in the regular app is the site's stop for that flag, set on the
  site node; the app says so.
- The console's back end is a federation peer of the sites it manages, not a database
  they share.
