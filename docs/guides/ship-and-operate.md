# Part 4: Ship and operate

For a contributor whose change has merged and who now wants it to reach people, and to stay up.
Reading this page takes about ten minutes. Then you rehearse a rollback.

**You end with** a rollback you rehearsed yourself, and the knowledge of how your merged change
becomes a release, reaches a node, and is kept healthy there.

**Before you start.** Finish [Part 3: Build a feature](build-a-feature.md): one change merged.
Your product's own Part 4 adds what is specific to it: its node, its site repositories' pins and
the exact deploy commands.

**Your agent reads a different page.** The [Part 4 agent reference](../reference/ship-and-operate-reference.md)
has every command, rule and failure. This page is for you.

## Contents

1. [Versions and tags](#1-versions-and-tags)
2. [Build and provenance](#2-build-and-provenance)
3. [Pins move forward only](#3-pins-move-forward-only)
4. [Keeping main green](#4-keeping-main-green)
5. [Deploy, and roll back](#5-deploy-and-roll-back)
6. [Operating: the agents that keep a node](#6-operating-the-agents-that-keep-a-node)
7. [The console gap](#7-the-console-gap)
8. [Do it: rehearse a rollback](#8-do-it-rehearse-a-rollback)

## 1. Versions and tags

A **version** names one release, such as `0.70.31`. A **tag** (`v0.70.31`) marks the commit on
`main` it was built from. A maintainer cuts a release with `axi release cut patch` (or `minor`,
`major`): it bumps the version, tags and pushes. You do not tag.

The tag is **annotated**: its message is the release notes. A tag without a message still
publishes, but people see no notes.

**Release trains** are being designed: products move to aligned major versions on a fixed
calendar, with a release between trains when something needs it. Until that is decided, a
maintainer releases when `main` has something worth shipping.

## 2. Build and provenance

A tag starts the publish workflow. It builds the wheel, installs it in a clean container to prove
it runs, and publishes it to the package index with a signed record of where it was built
(Sigstore attestations, ADR-109). Anyone can check that the wheel came from that tag.

So a change reaches a product **only through a release**. A product clone keeps running the
previous release until one is published and installed (`pip install -U axiom-os-lm`).

## 3. Pins move forward only

A site lists the exact versions it runs (a **pin**), so a clone reproduces what runs. A pin moves
forward to a release that exists and passed CI. It never moves back to match a node that drifted
from it: the node is wrong, not the pin. Fix the node.

## 4. Keeping main green

`main` is the shared branch every release is cut from. A green run is the expected state, and a red
one is an anomaly to read, not to retry.

- **The red-main rule.** When `main` is red, the pre-push hook refuses pushes without a written
  reason. The only standing exception is a push that fixes the failure itself.
- **Judge a commit by its own run.** A green run on an older commit says nothing about the commit
  at the head of `main` now. Check the run for the head commit.
- **Merge freezes.** When many pull requests land at once, each merge replaces the pending run on
  `main`, and no commit gets a finished run. Before a release, maintainers may stop merging until
  the tag exists. If you are told `main` is frozen, open your pull request but do not merge it.
- **Coming (#1782):** RIVET will judge each head by its own run, merge only on explicit success,
  land one pull request at a time, and hold a release lock that pauses merges from bump to tag.

## 5. Deploy, and roll back

A **deploy** moves a node to a new release. The platform's rule is that nothing we do takes a
service down (ADR-182). So a deploy is **blue-green**: the new release starts beside the old one,
is proven healthy, takes the traffic, and the old one is kept for **rollback**, which is the same
switch in the other direction (ADR-170). Migrations only add during a switch, so both releases can
run on one database.

Where a role does not yet run in two slots, the product's deploy command installs the new release
beside the previous one and moves a `current` link to it only once the install has succeeded.
Rolling back is deploying the previous version again; it is still on disk.

A rollback you have never rehearsed is a hope, not a plan. Section 8 is the rehearsal.

## 6. Operating: the agents that keep a node

Named agents watch a running node and propose fixes. You ask them; you do not have to remember
every command.

| Agent | Keeps | Ask it, or run |
|---|---|---|
| KEEL | The machine: disk, services, drift, worktrees. It absorbs TIDY and TRIAGE (ADR-230) | `axi node doctor`, `axi node vitals` |
| RIVET | CI, releases and landing pull requests | `axi release ci-health`, `axi release status` |
| KEEP | Secrets and their expiry | `axi vault list`, `axi vault audit` |
| GUARD | Permissions and the audit trail | `axi audit list` |
| HERALD | Notifications and alerts | Through the others |

`axi agents roster` lists every agent, and `axi agents ask <agent> "<request>"` asks one.

## 7. The console gap

There is no single operations console yet: no one page that shows every node, its version, its
health and its last deploy. The design is the [fleet console PRD](../prds/prd-fleet-console.md).
Until it is built, the commands in section 6 are the console.

## 8. Do it: rehearse a rollback

Use your product's deploy command, on your own computer, in a scratch folder. Nothing here touches
a real node. Your product's Part 4 gives the exact lines.

1. Deploy an older release into a scratch folder. Check that `current` points at it.
2. Deploy the newest release into the same folder. Check `current` again.
3. Roll back: deploy the older release again. Check `current`, and run the installed command's
   `--version` to prove which code is live.
4. Write down how long the rollback took. That number is what you promise when you deploy.

To see the full blue-green switch under load, read the latest `node-chart` workflow run: it deploys
a role from blue to green and back while traffic flows, and passes only with no refused requests,
no lost rows and no duplicates.

**You are done when** you rolled back, proved the version, and know how long it takes.

---

Next: [Part 5: The agentic layer](the-agentic-layer.md). Back: [Part 3](build-a-feature.md).
**Reference:** [Part 4 agent reference](../reference/ship-and-operate-reference.md) ·
[FAQ](../reference/faq.md) · [Guides index](README.md)
