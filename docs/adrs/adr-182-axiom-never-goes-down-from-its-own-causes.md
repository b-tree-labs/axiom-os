# ADR-182: Axiom never goes down from its own causes

**Status:** Proposed
**Date:** 2026-10-08
**Related:** ADR-052 (schema per extension), ADR-082 (composed serve), ADR-179 (a remote node updates by approval and swaps with rollback), spec-aeos-0.1 §6.5

## Context

Every Axiom service today is changed the same way: stop it, change it, start
it. That is a gap on each deploy, and a gap at every node on each fleet-wide
release. For most services the gap is seconds and nobody notices it. For a
collector reading at 20 Hz, one restart is a hole in the record that cannot be
filled afterwards. For an operator watching a live view, it is a blank screen
at the moment they are relying on it.

The platform already has most of the pieces, but nothing requires them and
nothing tests them together:

- The composed HTTP app has `/healthz`, which reports that the process is
  up. It does not report whether the app is ready to take traffic.
- `uvicorn` runs with `lifespan="on"`. No service declares a drain time, and
  no switch waits for one.
- The schedule extension already holds a single-flight lease (a Postgres
  advisory lock plus a lease row). Nothing uses that lease to hand work
  over during an upgrade.
- Thirteen extensions carry Alembic migrations. Nothing checks that a
  migration can be shared by the old and new versions at the same time.
- No test anywhere measures refused requests or lost readings across an
  upgrade.

A rule that holds in one extension and is unchecked everywhere else does not
hold. This ADR makes it uniform.

## Decision

### D1. The rule

**No deploy, update, schema migration or configuration change made by Axiom
takes a service down.** Outages caused by something outside Axiom (power,
network, hardware, a dependency's own outage) are out of scope for this rule.
They are covered by store-and-forward and by recovery, not by switching.

### D2. Every service declares how it switches

Every extension that provides a `service` declares `[extension.availability]`
(spec-aeos-0.1 §6.5):

- the switch strategy: `blue-green`, `overlap` or `stateless-rolling`;
- its readiness check;
- its drain time;
- whether its migrations are expand/contract;
- whether it is single-flight;
- the test that proves it.

`restart` is not an allowed strategy.

`axi ext lint` enforces this:

| Code | Severity | Fires when |
|---|---|---|
| AEOS100 | error | A service has no declaration. |
| AEOS101 | error | The declaration is malformed. |
| AEOS102 | warning | The declaration is made but its proof is pending. |
| AEOS103 | error | The declaration names a proof file that does not exist. |

Every built-in service declares from the day this lands, with its proof
pending.

### D3. One switch, used by every service

The platform provides the switch once, in the deploy and serve layer. No
service builds its own. The steps are:

1. start the new copy beside the old one;
2. wait for readiness;
3. move traffic;
4. let the old copy drain for its `drain_s`;
5. stop the old copy.

If readiness never arrives, traffic never moves and the old copy keeps
serving. The failed switch is reported and has cost nothing.

For a served HTTP app, the platform provides the switch as
`axiom.infra.switch.Supervisor`. It works the way a systemd socket unit or an
nginx binary upgrade does:

- The supervisor owns the listening socket.
- Every copy of the app accepts connections on that one socket.
- Connections the kernel has queued, but no copy has accepted yet, belong to
  the socket rather than to either copy, so they survive a switch.
- A new copy reports that it is ready over a pipe.
- A copy that is draining reports not-ready on `/readyz`, stops accepting new
  connections, reads the ones it has already accepted, and finishes its
  in-flight work.

`axi serve --supervise` runs a node this way, and SIGHUP triggers a switch.

`--supervise` is opt-in until the canary node has run under it. Once it has,
supervised serving becomes the default. Turning it on for every node at once
would itself be a change Axiom made, and an outage it caused would count as
our own.

For a single-flight service, the new and old copies both run during the
switch. Work does not stop, and it never runs twice, because exactly-once is
enforced for each piece of work by a durable claim. For the scheduler that
claim is the fire log's unique constraint on (schedule, time bucket,
parameters).

It is not enforced by a leader lease. The schedule extension's
`LeaseManager` is held in memory, and every process that holds one believes
it is the leader. It must never be the thing that prevents double work.

After the old copy has exited, the supervisor tells the new copy that it is
the only one (SIGUSR1). Only then does the new copy recover fires the old
copy left pending. Before that point, a pending fire that is only seconds
old may be the old copy's job, still running.

The data collector overlap in ADR-179 is an instance of the same switch.

### D4. Migrations are expand/contract

A schema change ships in two releases:

- **Expand:** add the new structure and backfill it, so the old and new
  versions both run against the same schema.
- **Contract:** in a later release, once nothing reads the old structure,
  remove it.

The CI guard is `axiom.infra.deploy.migration_guard`, run by the node-chart
workflow on every change to a migration (ADR-170). It checks the revisions
added since the latest release tag and reads only `upgrade()`. A contracting
revision must carry `# contract: <what> unused since <released version>`.

### D5. Proof is measured, not asserted

Each service's `verified_by` names a zero-gap upgrade test. The test
switches the service under load and asserts:

- zero refused requests;
- zero lost or duplicated readings or jobs.

CI runs these tests per role, on the same one chart that generates
Compose, K3D and k8s.

### D5a. Uptime is measured per installation and attributed to a cause

CI proves the switch works. Production has to show it keeps working. Each
installation (one node) records its own uptime from two independent
observers:

- **its heartbeat**, which says "I am up" from the inside;
- **the front door's probe of it**, which says "I can reach you" from the
  outside.

A downtime interval is any span in which either observer saw the node down.
Each interval carries both observers' readings, so "the node was down" and
"the node was up but unreachable" are told apart.

Every interval is attributed to exactly one cause:

| Cause | Kinds | How it is attributed |
|---|---|---|
| **ours** | deploy, update, migration, config | The interval overlaps a change Axiom recorded itself making: a switch, an ADR-179 update, a migration or a configuration change. Axiom writes those records before it acts, so the evidence exists whether or not the change succeeded. |
| **outside** | power, network, hardware | There is positive evidence, for example: a fresh boot with no shutdown record (power); the heartbeat continued, buffered, while the probe failed (network); a recorded device fault (hardware). |
| **unattributed** | none | Neither kind of evidence exists. |

"Unattributed" is its own cause and is never counted as outside. Without
that, every interval no one explained would quietly count as not our
fault. The claim would then hold because the evidence was missing, not
because it was true.

The record is aggregated per site node: all installations of one site, and
each interval keeps the installation it came from. The operator health view
shows it for each site over time.

**The claim "never down from our own causes" is measured as zero our-cause
downtime per site over the period shown, with the unattributed total shown
next to it.** A site whose unattributed total is not zero has not
demonstrated the claim, even if its our-cause total is zero. A reputation
for never going down is earned from this record, not claimed.

### D6. Ratchet

The declaration is required now. Proof is a warning until each service has
one, and becomes an error once the fleet has caught up. The list of
services still pending only gets shorter.

## Consequences

- **More resources during a switch.** For a few seconds, two copies of a
  service run. On a low-power node that is a real cost. The chart sizes for
  it, and a node too small to run two copies of its largest service is
  reported as not meeting the contract, rather than switched by restart
  without saying so.
- **Migrations take two releases instead of one.** This is the price of D4,
  and it is paid by whoever writes the migration.
- **Changes that cannot be made without a gap become visible.** Where a
  service cannot meet the rule (a single GPU that cannot hold two model
  copies, for example), the exception is written down in its declaration,
  not discovered during an outage.

## Alternatives considered

- **Restart quickly and call it good enough.** Rejected. Fast restarts
  still make gaps, and a 20 Hz reading lost in a gap does not come back.
- **Make each extension responsible for its own uptime.** Rejected. That is
  the current state, and it produced one extension that does it and twelve
  that do not.
- **Maintenance windows.** Rejected for Axiom's own changes. A window tells
  people when the platform will be down, and the rule is that it is not.

## Applied to every node role

The node roles are defined once, in `infra/charts/axiom-node` (ADR-170): the
edge, the collector, the local medallion, the forwarder, and the serving tier.
The chart applies D3 to each of them on every target it ships to: Compose
generated from the chart, K3D, and Kubernetes.

- **Edge and serving tier: two slots behind one front.** A role with
  `blueGreen` runs a slot per release. The slots share the role's data volume;
  the edge's outbox is safe for two writers. Each slot has its own installed
  packages. The front owns the listening port:
  - on Kubernetes, a Service, where moving traffic changes only its selector,
    so no pod restarts;
  - on a single host, nginx, which reloads gracefully.

  `axiom.infra.deploy.bluegreen` performs D3's steps: the idle slot ready →
  both slots → the new slot. The old slot is kept, so rollback is the same
  switch back. Inside a slot, a served app runs under the D3 supervisor
  unchanged. The front decides which slot gets traffic; the supervisor
  decides which process inside a slot serves it.
- **Collector: an instance of the same switch** (ADR-179). The old and new
  copies run side by side. Each reading carries an identity that is the same
  from both, so every consumer (the realtime lane and bronze) drops repeats.
  The chart gives each copy its own journal on the role's volume.
- **Local medallion and forwarder.** The forwarder is single-flight. Exactly
  once is enforced by D3's durable claim, never by an in-memory lease. Its
  successor takes over after the old copy exits. The medallion's database is
  shared infrastructure (ADR-170 decision 7), so migrations against it follow
  D4.
- **D4 enforced in CI.** `axiom.infra.deploy.migration_guard` checks the
  migrations added since the last release tag. A drop, rename or NOT NULL
  needs `# contract: <what> unused since <released version>`, naming a version
  that is already released.
- **D5 for the roles.** `infra/smoke/zero_gap_upgrade.py` switches the edge
  blue → green → blue under live load on Compose and on K3D. It asserts zero
  refused requests, and that the downstream receives exactly the rows sent
  (no loss) with none twice. First measured on 2026-10-08:
  - Compose: 6,580 rows, 0 refused.
  - K3D: 7,140 rows, 0 refused.
- **Change records.** Every switch records a `deploy` change before acting,
  as every other D3 switch does.
