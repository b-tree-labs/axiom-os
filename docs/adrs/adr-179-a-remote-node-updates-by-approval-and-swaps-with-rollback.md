# ADR-179: A remote node updates without going down, by policy and approval, with rollback

**Status:** Proposed (revised 2026-10-08: blue/green overlap, never down from our own causes; neglect-safe defaults)
**Date:** 2026-10-08

**Revision, 2026-10-08.** Two decisions folded in before acceptance:
blue/green with overlap is how every update and deploy happens (§0), and a node
left alone for months must stay safe (§2, §7, §8, §9). The earlier text's
default policy of `approve` is replaced by `auto-patch`.


**2026-10-08 (ADR-180):** an update to a producing node must not lose the local medallion or the forwarder's position. The self-check after a swap includes local bronze accepting a write and the forwarder resuming from its acknowledged cursor.

## Context

A node installed at a partner site (a data-acquisition collector on modest
hardware, often in a container) has to take updates routinely and quickly, and
the people who run it should barely have to be involved. Today:

- The update extension offers an update when a person runs a command at a
  terminal, and shows the release notes of every release in between. A node
  that runs unattended never sees that offer.
- `Updater` upgrades the running environment in place. If an install or a
  migration fails halfway, the node is left half-old, half-new, with nothing to
  go back to.
- A container image pinned at build time means "update" is a rebuild, which a
  partner has to do by hand.
- HERALD can notify, but cannot yet carry an approval that is safe to act on
  (see "HERALD today" below).
- The node cannot be reached from outside. Anything that tells it to update has
  to be something it fetches, over an outbound connection.

The partner wants to keep control (see what changed, say yes or not now) without
having to do the work.

## Decision

### 0. Never down from our own causes

Deploys, updates, migrations and configuration changes cause zero downtime.
Outside causes (power, network, the platform's host) are covered by
store-and-forward: data is delayed, never lost.

For a collector this means an update is an **overlap**, not a restart:

1. green (the new version, built beside blue in its own environment, §1) starts
   as its own instance and reads the same source while blue still runs;
2. both publish for a while. Each instance has its own journal and its own hash
   chain; storage drops the doubled readings by the reading's identity (the row
   without its chain fields, with the producer's instance suffix removed), and
   realtime messages carry that identity as `key`, so a consumer subscribed to
   every live instance drops repeats;
3. green is verified live: readings arriving on its realtime lane and its
   journal being sent;
4. then blue is asked to stop, and sends everything it journaled before it
   exits. Rollback is green stopping; blue never stopped.

State during the overlap: each instance owns its journal, cursors and chain,
so nothing is shared by two writers. A source with a durable cursor (a folder
watch, an archive database) is read by one instance at a time and handed over
at cutover from the persisted cursor. A local medallion has a single writer:
the instance that owns the cursor.

Proven with real processes: a 20 Hz EPICS counter, a real ingest edge, two
separately installed versions, a switch and a mid-switch rollback; 422 of 422
readings at the realtime consumer and in bronze, none missing, none doubled.

### 1. Each version is its own environment; the switch is one rename

The node's persistent storage holds `venvs/<version>/` per installed version and
a `current` symlink to the one in use. Whatever runs the node (a container's
entrypoint, a scheduled task, a service) always executes through `current`, so
the container image never changes for an update.

An update (`axiom.extensions.builtins.update.swap.apply_update`):

1. builds the new environment beside the running one, at its final path
   (environments are not relocatable: their scripts name the path they were
   built at). A failed install changes nothing and is removed;
2. runs the pre-switch checks inside the new environment: the CLI answers, the
   node's own readiness check, one dry pass of its configured source;
3. switches `current` with one atomic rename;
4. runs the post-switch health checks through `current`, and switches back if
   they fail;
5. keeps the previous environment, so a manual rollback is also one rename;
6. appends the outcome to `update-log.jsonl`, which the node's heartbeat reports.

### 2. The site chooses the policy; the default takes fixes and asks about features

```toml
[update]
policy = "auto-patch"     # auto-patch (default) | approve | hold
window = "02:00-04:00"    # local time; optional
```

- `auto-patch` (default): fixes and security releases (patch releases of the
  running minor) apply on their own inside the window, with checks and rollback;
  anything that adds features waits for approval. A node nobody looks at for
  months still receives every fix.
- `approve`: every release waits for a person.
- `hold`: nothing applies until the site changes this. Policy never chooses a
  downgrade; a rollback is the only way back.

### 3. What a node may take comes from a signed release channel it polls

The platform operator publishes a **release channel**: a small manifest listing
which versions a node class may take, each with its release notes and the
digest of the artifact. It is signed with the operator's release key, and served
from the public intake host the node already reaches. The node polls it
outbound, verifies the signature, and only ever installs a version listed there.
A version can be on PyPI and still not offered to partner nodes until the
operator promotes it.

### 4. Approval is a signed one-time link, delivered by HERALD; the answer comes back through the channel

When a version waits for approval, HERALD sends the site's chosen contacts a
message on their preferred channel (email first; Slack and SMS where
configured). It contains the release notes of every version between the running
one and the offered one, and two links: **Approve** and **Not now**.

Each link carries a token signed by the platform. The token is bound to the site,
the node, the version, the recipient and an expiry, and is usable once. Opening
it records the decision against the recipient it was issued to. The next
channel poll carries the approval to the node, which applies it in its window.

The approval travels to the node through the maintenance relay it already
polls (ADR-183). The node verifies the signature, the binding (site, node,
request, and that the digest of the action matches what it holds pending, so a
link cannot approve a request that was altered after it was issued) and the
expiry, then approves through the same path as its command line
(`MaintenanceNode.approve(request_id, by=recipient)`). Applying an update is one
allowlisted action among others.

**A reply to an email is never the proof.** The `From` header is not
authenticated, and a keyword like "yes" carries no binding to what was asked.
Reply parsing may stay as a convenience that answers with the signed link; it is
not an approval by itself.

### 5. Under "assist", a maintenance agent may propose; only a person approves

A site declares `agents = "none" | "assist" | "local"` in its node file (the site agent
policy, spec-aeos-0.1 §6, Axiom #1199). Under `none` and `assist`, nothing on the
site's machine runs an agent or calls a language model; the gateway refuses
before sending, and autonomy settings cannot override it.

A site that declares `assist` keeps its machine as quiet as `none`. The
difference is that a maintenance agent running on the platform operator's side
reads the node's heartbeat (versions, lane health, disk, backlog, errors) and
may **propose** an action: apply an approved update, rotate a key, raise
retention, restart the collector. A proposal travels through the same approval
channel as updates: a notice with a one-time approval link bound to the
recipient and the action. The node carries out only the deterministic action a
person approved, using the same updater or command a person would run, and
reports the result in its heartbeat.

The agent never has a session on the machine, never runs code there, and cannot
approve its own proposal. Declining or ignoring a proposal changes nothing.
Under `none`, no proposals are sent. Under `local`, agents may also run on the
machine once turned on.

Changing the policy is a site decision made at the machine's own command line
(`features agents <policy>`). It is never exposed over MCP or any remote
surface, so no agent can widen it.

### 6. An unanswered approval climbs a ladder

| Day | Channel | To |
|---|---|---|
| 0 | email | the site's contacts: what changed, and the links |
| 7 | email | the same contacts: a reminder |
| 10 | chat | the site's chat channel, where it has one |
| 14 | dashboard | a flag in the node's heartbeat the operator sees |
| 21 | email | the platform operator: contact the site directly |

A contact whose address bounces permanently is marked departed and not tried
again; with nobody left, the operator is told at once. An answer stops the
ladder. State is one file per pending approval, so it survives restarts.

### 7. Out of date never loses data

An intake declares the oldest sender it accepts (for example
`collector-pkg>=1.17.1`); every sender names what it runs on each request. A
sender below the window is answered 426 with the requirement and the exact
update command, before any budget is spent. 426 because the statuses a sender
treats as definitive discard rows. The sender holds: rows stay journaled, its
backoff does not escalate, it asks again hourly, and its status says "update
required" with the command. Once updated it sends everything it held.
Heartbeats are still accepted, so an old node stays visible.

### 8. A neglected node survives what it meets

- Power lost mid-update: the update records what it is doing first, and the
  next start finishes or undoes it.
- A full disk: refused before anything changes; a disk that fills mid-install
  leaves the running version untouched.
- The package index unreachable: retried at each window; a person is told after
  a set number of days.
- A release needing a newer Python: refused with the exact command that
  installs one without root, and taken with that interpreter.
- An update during a running pass: the environment a pass is using is never
  removed under it.
- Skipping releases: a node upgrades from any recent release straight to the
  current one, on the state the old release left, without loss or duplication.

### 9. Nothing waits on this to exist

Until the channel and the approval links are deployed, an update is one command
the site runs when the platform operator asks, and §0, §1 and §8 still apply.

## HERALD today (surveyed on main, 2026-10-08)

| Capability | State |
|---|---|
| Outbound email | Present: SMTP, Resend, SES, ACS, Gmail and Microsoft 365 providers behind one channel. A sender domain still has to be chosen and verified with whichever provider is used. |
| Inbound email reply | Partly: `parse_email_inbound` maps a first line of yes/no to a **generic** `confirm`/`reject`, with the actor taken from the unauthenticated `From`. Not bound to any request. No IMAP poller or provider inbound webhook is wired. |
| Slack | Outbound webhook channel present. An interactive Socket-Mode channel exists (outbound websocket, so it works behind a firewall); its live client needs the Slack SDK and a bot token. |
| SMS | Twilio, ACS and SNS outbound present; a Twilio interactive channel exists. |
| Inbound webhooks | One route, `/herald/inbound/{vendor}`, with a verifier scaffold. **No vendor verifier is registered.** |
| Approval bridge | `approval_bridge.notify_pending` reduces channel answers into `ApprovalGate`, the single record of who approved what. **Nothing calls it outside its own module.** |
| Signed approval links | **Absent.** Nothing issues or verifies a link bound to recipient, subject and expiry. |
| Release channel | **Absent.** |

## Consequences

- No update, deploy, migration or configuration change takes a node down; the
  realtime stream and storage see no gap across a switch or a rollback.
- An update never leaves a node half-applied, and a bad release costs one
  rollback, done automatically.
- The partner sees what changed and answers with one click; under the default
  `auto-patch` they need not answer at all for fixes, and silence escalates to
  the operator instead of leaving a node out of date.
- A sender too old for its intake keeps every reading and says so, rather than
  sending data an intake can no longer read correctly.
- New pieces to build, in order: the release-channel manifest and its signing;
  the token issue/verify endpoint on the public host; the approval-link rendering
  in HERALD's update message; the node's poll-and-apply loop with policy and
  window; the heartbeat field.
- The public host gains one more read path (the channel) and one write path (the
  approval endpoint). Both are small and authenticated by the signed token, not
  by a session.
- A container image is built once per Python and base-OS change, not per release.

## Alternatives considered

- **Automatic updates to every release.** Simplest, but it takes the decision
  away from the site, and a bad release reaches every node at once.
- **Rebuild the container per release.** Familiar, but a partner has to rebuild
  or pull, which is exactly the effort this avoids.
- **Approve by replying "yes" to an email.** Easy to read, but unauthenticated
  and unbound, so not something an unattended node should act on.
- **Push updates to the node.** The node is not reachable from outside, and it
  should not be.
