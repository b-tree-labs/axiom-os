# ADR-183: A site is maintained by signed requests it fetches and sessions it opens, never by a login

**Status:** Proposed (2026-10-08)
**Related:** ADR-164 and spec-aeos-0.1 §6 (the node declaration this extends), ADR-177 (the ingest edge the relay rides), ADR-179 (approval links and the `assist` agent policy), [ADR-106](adr-106-push-ingest-gateway.md) (tenancy: the site comes from the credential), `src/axiom/infra/maintenance.py`, `src/axiom/infra/support_session.py`, `src/axiom/extensions/builtins/data_platform/ingest_sink/maintenance_box.py`

## Context

The platform operator has to maintain the nodes that run at other organizations'
sites: restart a stalled collector, collect a support bundle, apply an update,
re-read a window of history, and now and then look at the machine with a person
there. The usual answers are a VPN, an SSH account, or a remote-desktop tool.
Each fails the same constraints at a partner site:

- **No inbound ports.** A site's firewall allows outbound HTTPS and little else,
  and its information security office will not open a port for another
  organization.
- **No standing account** for the operator on the site's machine.
- **Every connection starts on the site's machine.**
- **Everything is audited**, on both sides.
- **The site can see it and stop it**, without asking the operator.

A site's appetite also differs. One wants nothing remote at all; most will accept
narrow, pre-agreed actions; a few want the option of a supervised session when
something is badly wrong.

## Decision

### 1. A site declares a maintenance level, the way it declares its agent policy

`node.toml` gains `maintenance = "off" | "requests" | "sessions"` beside
`agents = ...`:

| Level | Meaning |
|---|---|
| `off` (0) | Nothing remote runs. The node still reports its health. **The default when undeclared.** |
| `requests` (1) | Signed requests for actions on a fixed allowlist. Read-only actions run; changes wait for a person at the site. **The default a partner role declares.** |
| `sessions` (2) | As level 1, and a person at the site may open a time-boxed, recorded support session. |

Changing it is `features maintenance <level>` at the machine's own command line.
Like the agent policy it is never offered over MCP or any remote surface, so
nothing remote can widen it. `features maintenance off` stops everything at once,
and the generated "What runs on this machine" page states the level in force,
with a drift check that fails when the definition declares a level the published
page does not say.

### 2. Level 1: signed requests from an allowlist, fetched by the node

**The allowlist is code.** `diagnose`, `support_bundle`, `restart_service`,
`apply_update`, `set_setting` and `rerun_backfill`, each with a parameter schema
(each value must fully match a pattern; no extra parameters). Adding an action is
a reviewed change to Axiom, not a configuration.

**What an action does on a given machine is the deployment's.** A wiring file
(`maintenance.toml`) that ships with the role's definition maps each action
(per target, for `restart_service` and `set_setting`) to a fixed argument list.
Parameters are substituted as whole arguments and nothing is run through a
shell. An allowed action the deployment did not wire is refused. Commands run as
the account that runs the node's maintenance service, which is the role's
service account, never root. A command that needs more (a system unit restart,
say) gets a sudoers line for exactly that command, written in the role's
definition where an information security office can read it.

**The request is signed by an operator key** (Ed25519, `axiom.vega.identity`).
The canonical bytes are the sorted-key JSON of
`{id, site, action, params, issued_at, expires_at, operator, key_id}`. The node
holds the public keys it trusts, each bound to one operator principal, in its
wiring. The private key is created into the operator's vault
(`maintenance keygen`) and never leaves it.

**The node is the authority, not the relay.** It refuses, and records why, any
request that:

- is signed by a key it does not trust, or whose signature does not match the
  bytes (`unknown_key`, `bad_signature`; any edit after signing, including
  re-addressing it to another site, lands here);
- is addressed to another site (`wrong_site`);
- has expired, is not yet valid, or claims a validity longer than 24 hours
  (`expired`, `not_yet_valid`, `validity_too_long`; five minutes of clock skew
  is allowed);
- has been seen before (`replayed`; the seen-set is on disk, so it survives a
  restart, and entries are pruned once they could no longer be valid);
- names an action off the allowlist, or parameters outside its schema
  (`unknown_action`, `bad_params`);
- names an action the deployment did not wire (`not_wired`);
- arrives while the site has maintenance `off` (`maintenance_off`).

**Delivery is the node's existing outbound path.** The edge (ADR-177) gains a
mailbox at `/maintenance/` on the same server and the same nginx front as
`/ingest/` and `/edge/`. An operator posts a signed envelope. The relay files it
under the site the envelope names. The site's node fetches its own site's queue
with its own key and posts results back. The site comes from the node's
credential, never from a payload, so a node can neither read another site's
requests nor post another site's results. Only principals listed in
`AXIOM_MAINTENANCE_OPERATORS` may post requests or read results, and a site's
key can never post a request. The relay never needs to trust a request, because
it never runs one.

**Approval.** Each action has a risk: `read` (diagnose, support bundle) or
`change` (everything else). By default read-only actions run on arrival and
changes wait. A site may set `approve = "all"`, so that everything waits, or
`"none"`. A waiting request runs only after a person at the site approves it
(`maintenance approve <id>`), and it is dropped if nobody does before it
expires. Denying or ignoring a request changes nothing. When ADR-179's signed
one-time approval links exist, an approval made through a link is carried to the
node on its next fetch and enters through the same `approve` path, bound to the
request's id, site, recipient and expiry. Until then the site's command line is
the approval surface.

**Results and audit.** Each decision (`received`, `refused` with its reason,
`pending`, `approved`, `denied`, `expired`, `done` or `failed` with the exit
code) is appended to the node's audit log with the request id, action,
parameters, operator and key id. Results, including the last 16 KB of output,
are posted to the relay, where the operator reads them and they form the
platform's side of the audit. The node's heartbeat carries a summary: the level,
the approval rule, how many requests are waiting, and the last ten outcomes.

### 3. Level 2: a session the site opens, over the same relay, as plain HTTPS

`support open --for 2h` is run by a person at the site. Nothing the operator can
send opens a session: the relay accepts a new session only from the site's own
node, and the node opens one only at level `sessions`.

The node asks the relay for a session over its outbound HTTPS connection, starts
a shell in a pseudo-terminal, and carries the terminal's bytes both ways by
long-polling (a fetch waits at most 20 seconds). The shell runs as whoever
opened the session, or as the account the wiring names (`support_shell`, for
example a `sudo -u` line for the role's service account), and never as more.
The person who opened it sees everything typed and printed, live, in their own
terminal. The session ends when its time is up (at most eight hours), when the
shell exits, when they press Ctrl-C there, or when anyone at the site runs
`support close`. After that the relay refuses input (410). Both sides keep an
asciicast v2 recording: the relay's, and the node's own, under its state
directory, which belongs to the site. Opening and closing are in the node's
audit log.

**The three transports considered:**

| | SSH reverse tunnel | **HTTPS relay on 443 (chosen)** | WireGuard / Tailscale |
|---|---|---|---|
| Inbound port at the site | none | none | none |
| What the site must allow out | TCP 22 to the relay, often blocked | HTTPS to the host it already sends data to | UDP 51820 (WireGuard), or a third-party coordination service and its relays (Tailscale) |
| What the relay host must expose | an sshd for partners, a new public service on the UT host | one more `location` in the nginx it already runs | a UDP port (WireGuard) or nothing (Tailscale) |
| What it grants | a port forward into the machine; a shell only by convention | one shell, one session, one site | network-layer reach to the machine's every port |
| Recording | possible with a forced command on the relay | both ends, by construction | none inherent |
| How an InfoSec office reads it | "a reverse SSH tunnel to another organization", the textbook backdoor | an outbound HTTPS client to a known host, started and stopped by a person here | a VPN into another organization's network, usually prohibited |
| New external dependency | none, but a new public sshd | none | a kernel module and root (WireGuard) or a vendor and an account (Tailscale) |
| Survives a proxy that strips `Upgrade` or inspects TLS | no | yes (no WebSocket upgrade needed) | no |

**The relay is plain HTTPS long-polling rather than WebSockets.** A WebSocket
relay would also run on 443, but it needs `Upgrade` headers in the edge's nginx
block and a WebSocket library on both ends: base Axiom ships `uvicorn` without
one. It also fails behind proxies that strip the upgrade or inspect TLS, which
are common at institutional sites. Long-polling costs a little latency, which
does not matter at typing speed. It needs nothing the edge and the node do not
already have. WebSockets can be added later as a faster transport behind the
same session API, if that is ever worth it.

**A session needs a POSIX pseudo-terminal**, so level 2 is for Linux and
macOS nodes. A Windows node declares `requests` at most; `support open` there
refuses in words, and its heartbeat and status still report the maintenance
state. Level 1 is plain Python and runs wherever the node runs.

### 4. Where it lives

Generic, in Axiom:

- the request protocol, verification, approval and execution:
  `axiom.infra.maintenance`;
- sessions: `axiom.infra.support_session`;
- the relay: `ingest_sink.maintenance_box`, mounted by the `ingest-edge`
  profile wherever an outbox is configured;
- the verbs: the `maintenance` builtin (`maintenance`, `support`, always
  visible, like `status` and `update`).

A consumer's role definition supplies the wiring: trusted keys, the commands
behind each action, the session shell, and its sudoers lines.

## Consequences

- A partner's information security office reviews one outbound HTTPS client, a
  short allowlist, a wiring file that says what each action runs, and a switch
  they own. There is no port, no account, no VPN and no third-party service.
- The operator cannot do anything at a site that the site did not wire, and
  cannot change anything without a person there agreeing. Level 2 is something
  the site does for the operator, not something the operator does to the site.
- A compromised relay can withhold or delay requests, and can see results and
  session bytes, but cannot forge, alter, replay or re-address a request, and
  cannot open a session. Results and session bytes ride HTTPS to UT's own host,
  the same trust the data path already has.
- A compromised operator key can request allowlisted actions at the sites that
  trust it: read-only ones run, and changes still wait for the site. Rotation
  means replacing the public key in each site's wiring. A key-distribution
  channel is follow-up work.
- Sessions live in the relay's memory. Restarting the edge ends every open
  session, and their recordings remain. Running the edge with more than one
  worker process would need the session state moved to shared storage.
- A node that dies mid-session (a crash, a power cut) leaves its session file
  behind. The file carries a lock that dies with the process, so the next
  `support` command and the heartbeat report the session as interrupted rather
  than open, nothing signals the recorded pid (after a reboot it may belong to
  anything), and the next `support open` ends the interrupted session at the
  relay before starting its own. A node holds one session at a time.
- The edge's first deployment ships without the relay, exactly as tested (the
  edge bundle in #1194). `/maintenance/` arrives in a later edge update through
  the node chart: the nginx block, `AXIOM_MAINTENANCE_OPERATORS`, and the
  `maintenance:read`/`maintenance:invoke` scopes on the site keys that take part.
  Until then every site is effectively at level 0.
- The prototype proof ran the relay as a second container beside the running
  edge on the replica (`127.0.0.1:8788`). In the package it is the same
  container: `/maintenance/` is one more route of the edge, and the nginx block
  points at the edge's own port.

## Proof (2026-10-08)

- **Unit, with real commands:** forged, tampered, untrusted-key, replayed
  (including after a node restart), expired, not-yet-valid, over-long, other-site,
  disallowed, bad-parameter, unwired and maintenance-off requests are all
  refused. A read-only request runs. A change waits, runs on approval, never
  runs on denial, and expires unapproved. Parameters arrive as single
  arguments. The heartbeat summary is correct.
- **End to end, with real processes:** an `ingest-edge` server process with
  issued keys and the node's real `maintenance` and `support` commands in their
  own processes. The tests cover relay access control, the same refusals across
  the wire, approve-then-run, maintenance off, and a real shell session opened
  by the node, driven by the operator, ended by `support close` and recorded on
  both sides.
- **On the replica of the production edge host,** through its real nginx site
  file and TLS, from a partner container whose only connection was outbound to
  443:
  - every class of bad request was refused at the node, including the
    untouched other-site request that a misfiling relay was made to deliver;
  - a replay was refused;
  - the change waited until the site approved it, then ran;
  - in a session the operator ran commands while the site watched;
  - the site ended the session from another terminal, after which the
    operator's input was refused (410);
  - a one-minute session left alone ended itself after 61 seconds.
