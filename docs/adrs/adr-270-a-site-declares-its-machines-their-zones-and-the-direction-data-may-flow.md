# ADR-270: A site declares its machines, their zones and the direction data may flow

**Status:** Accepted (2026-11-20; drafted 2026-11-17; revised 2026-11-19 after the first install was redone on the standard chart)
**Related:** ADR-164 (a node declares its functions), ADR-177 (an ingest edge is pushed to and pulled from), ADR-179 (updates by approval), ADR-180 (a producing site keeps a local medallion), ADR-182 (never down from our own causes), ADR-215 (deployment sizes), ADR-266 (readings from several sources share one timeline)

## Context

A site is the unit of deployment, and a site is often several machines. ADR-164 lets one node say
which functions it has. Nothing says which machines make up a site, how they depend on each other,
or which of them may open a connection to which.

Three shapes are in service or being installed:

- **One machine.** A partner's collector that reads instruments, buffers and sends.
- **Three machines with ordinary networking.** A realtime processor that reads the instruments and
  hosts a model, a storage buffer, and a developer's workstation.
- **A control site.** A machine that runs a control law and may move an actuator. During operation
  it is isolated: measured data reaches it one way, its records leave one way, and people reach it
  only in a maintenance window. A second machine outside that boundary shows operators what is
  happening and relays records onward.

The third shape was first installed by hand, and the first design for it put a connection from the
less trusted machine into the control machine on the realtime path. Nothing in the platform said
that was wrong, because the platform had no place to say what the site's boundaries are.

## Decision

**A site's configuration declares its machines.** For each machine: a role name, the functions it
has (ADR-164), the zone it is in, what it can reach, the deployment size it runs at (ADR-215),
and how software arrives (online, offline bundle, a signed update in a maintenance window).

**Zones and conduits.** A site names its zones in order of trust; the reference set is `control`,
`site` and `off-site`. Every path between zones is a declared **conduit** with a direction and a
state:

- **one way**, for data: into the control zone from sources, and out of it to the site zone. A
  conduit may be a hardware diode or an enforced one-way link; the declaration says which.
- **maintenance window only**, for people and updates: closed during operation, opened by
  approval (ADR-179), and its state is itself a reading that functions can gate on.

Two rules follow, and the platform refuses a declaration that breaks them: nothing in a less
trusted zone opens a connection into a more trusted one except through a maintenance-window
conduit, and
no function in the control zone depends on a conduit that is closed during operation.

**The function catalog gains the roles these sites need:** source tap, realtime processor
(collector, timing coordinator, model host, controller), storage buffer (landing, local medallion,
spool for the outbound conduit), relay, monitoring display (fed one way; it cannot command and
shows its own data age), command station (inside the control zone, wherever in the building its
screen sits), and developer.

**Every machine installs the standard way; the control zone is the one narrowed case.** A machine
outside a control zone is an ordinary node: the node chart at its size, with the chart's own
blue/green slots. A first install that used signed packages in virtual environments on every
machine had to re-solve updates, rollback and configuration by hand, and was redone on the
chart. Functions in a control zone ship as one small, signed image pinned by digest, with a short
dependency list, run without a cluster: a cluster beside a control loop adds a large trusted base
and timing jitter. Its signature can be checked with no network. An update there is a verified
image, a switch and a kept last known good; where the function drives an actuator, the new
release runs in an idle slot in shadow and is proven before it takes over, and only one slot
ever holds the actuator.

**Settings are in three layers, and an update writes only the first:** what ships in the release;
the site's declaration; and what was set on the machine. An install refuses to overwrite the
third unless told to, and backs it up first. Every update reports what it kept.

**Install and check from the declaration.** One command builds each machine's bundle from the
site's pinned versions. A preflight on the machine verifies what was declared (reach, the
direction of each conduit, clock source, disk, cores) before anything starts, and the result is
recorded.

**One view of the site, built from what flows one way.** Each machine emits status frames on its
outbound conduit. A machine that is isolated by design shows as isolated, with the age of its last
frame, never as down.

**Protected details stay out of the declaration.** Hostnames, addresses and accounts live in a
private overlay or the vault. The declaration names roles and zones only, so it can be reviewed
and kept in a repository.

## Options considered

- **Keep describing one node at a time.** It is what allowed a design that reached into the control
  machine. A boundary nobody declared is a boundary nobody checks.
- **Assume one orchestrator spans the site.** Control machines and instrument hosts often cannot
  run one, and a zone boundary is exactly where an orchestrator may not reach.
- **One large node per site.** It puts storage and web software beside a control law, and it cannot
  describe a machine that is cut off while the plant runs.

## Consequences

- The one-machine site is the same declaration with one entry; nothing changes for it.
- New work: the declaration and its validation, the per-machine bundle builder (including the
  offline and signed forms), the preflight, the control-zone image and its shadow slot, the status
  frames and the site view. What the chart could not do for an isolated machine at the first
  install: a way in that is not HTTP, an offline package source, a cluster base that needs no
  internet, and slots that carry a role's companions as well as its main process.
- A site's own process for software in its control zone (review, coverage, signatures) attaches to
  the functions declared there; the platform supplies the packaging and the evidence, the site
  owns the rules.
- Fleet features (ADR-260) reach a control zone only as a signed update in a maintenance window,
  never as a live push.
