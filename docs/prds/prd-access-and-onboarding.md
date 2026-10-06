# PRD: Access and onboarding

**Status:** Living
**Owner:** platform
**Related:** [ADR-151](../adrs/adr-151-roles-separate-governing-from-reading.md) (roles),
[ADR-045](../adrs/adr-045-raci-evolution.md) (what may act unattended),
[ADR-075](../adrs/adr-075-sso-oidc-delegated-auth.md) (SSO and OIDC),
a consumer layer's own decision that off-node access is token-served

## Why this exists

Six people onboarded onto a node on 2026-10-01 and none reached a first
answer without help. Nothing in the data path failed. What failed was
everything around it: a question asked before the information needed to
answer it, thirteen connections offered before any were needed, two access
tokens echoed to a screen as they were pasted, a system service installed
behind a default-yes, and a credential only one person in the building
could issue.

This document states what the access and onboarding surfaces must do, in
terms that can be checked. The flow it describes is held in a design document under
`docs/working/`; the role vocabulary it depends on is ADR-151.

## Principle

**Ask for what the task needs, when it needs it.** Every requirement below
is an application of that sentence, and every failure above was a
violation of it.

## Requirements

### R1 — A newcomer reaches a first answer without another person

One command after install, and the only wait is a restart of their editor.
The surface states, before it does anything: what it will do, that nothing
runs in the background, that nothing is installed outside the virtualenv,
and how to undo it. It ends on an answer about the data, not on a summary
of connections that did not work.

- **R1.1** No step asks a question whose answer requires information the
  surface has not already given.
- **R1.2** No step installs a service, creates a user, changes group
  membership or downloads a model without naming that act and defaulting
  to no.
- **R1.3** A step that fails stops or is recorded as unfinished. The
  closing summary does not report success it did not achieve.
- **R1.4** The surface detects the harness and says, in its own words, that
  the harness launches the server itself and must therefore be restarted.
  It then verifies that the harness can reach the node, and names which of
  config, restart or credential is at fault when it cannot.
- **R1.5** The commands it prints are correct for the shell it is running
  in, and it says that the install shell and the editor's shell must match.

### R2 — No human handles a credential

- **R2.1** No prompt that reads a credential echoes it.
- **R2.2** The onboarding path obtains a key without a person reading,
  pasting or carrying one: the CLI starts an activation, the person signs
  in in a browser, the CLI receives the key and writes the configuration.
- **R2.3** A key's scope comes from server-side policy keyed to the
  requester's role. A self-service caller asks for "a key" and cannot ask
  for a scope.
- **R2.4** A person can see and revoke their own keys, and only their own.
- **R2.5** Revocation takes effect on the next request, with no restart.

### R3 — An administrator can admit people without a terminal

- **R3.1** A person is invited **by email address**, before they exist as a
  principal, and the invitation carries the role they will hold.
- **R3.2** Collisions are resolved at invite time, not discovered at sign-in:
  an address that is already a password account, already holds an
  unconsumed invitation, already exists under another provider, or differs
  only in case or by a plus-tag. Each has a stated answer. Guessing is
  refused.
- **R3.3** An attempted self-sign-up is recorded, so that an administrator
  inviting that address later can see it happened.
- **R3.4** Invitations can be issued one at a time, from a list, and from
  chat. These are front doors onto one verb.
- **R3.5** Default role for an invitation is the floor. Elevation is
  explicit, per invitation.
- **R3.6** A node has an owner from installation, who can appoint
  administrators. Every grant is an audited act naming a human.

### R4 — Two ways in resolve to one principal

- **R4.1** Issuer and subject are the primary identity key; a verified
  email is the secondary key.
- **R4.2** An unverified email never links anything. A provider asserting
  an address it has not verified is treated as not having asserted it.
- **R4.3** A password account and an OIDC identity for the same verified
  address are one principal with two credentials, never two principals.
- **R4.4** When self-signup is disabled the gate admits nobody who was not
  invited, by any route. "Invitation only" and "self-signup enabled" cannot
  both be true.

### R5 — The public floor is an allowlist and is delayed

- **R5.1** The public face is a separate read-only page, not a tier of this
  application.
- **R5.2** Everything on the floor tier is time-delayed, including
  predictions, by a property of the tier rather than per surface.
- **R5.3** A newly mounted surface is not visible to the floor tier until
  somebody publishes it.
- **R5.4** A person refused access is told what they can see, that access is
  by invitation, and who to ask. A refusal is not a stack trace.

### R6 — Agents do the work that does not need a person

Per ADR-045 D6, placed by reversibility:

- **R6.1** Diagnosing a stalled onboarding, walking the documented install
  path and reporting roster drift are unattended.
- **R6.2** Issuing a floor key against an accepted invitation, re-sending an
  expired invitation and revoking a long-unused key act then notify, with a
  24-hour undo.
- **R6.3** Inviting, elevating to creator and revoking a key in use ask every
  time, through email, chat or the CLI.
- **R6.4** Granting admin, operator or owner, publishing to the floor tier,
  and accepting a peer's role claim never graduate past asking.

## How this is proven

Requirements that cannot be checked are preferences. Each one above has a
test, and the tests need three pieces of infrastructure we do not have.

### The infrastructure

**A fake OIDC issuer, in process.** Serves a discovery document and a JWKS,
signs ID tokens, and lets a test dictate the claims: a missing `email`, an
`email_verified` of false, a wrong `aud`, an expired token, a rotated key,
a clock skew, a nonce that does not match. This is where providers actually
differ, and it needs no account anywhere. It is also what lets us claim
support for any conformant issuer rather than for two we happened to try.

**A clean-machine harness.** A container per supported operating system, in
which the documented install path is executed from an empty state. The
commands come from the published guide rather than from a test fixture, so
a guide that drifts fails the suite rather than failing a newcomer.

**A fake MCP client.** One that calls everything the server advertises:
every tool, every resource, every prompt. The prompt defect found on
2026-10-01 — every advertised prompt listed and none fetchable — survives
any test that only checks the listing.

### The tests

| # | Scenario | Proves |
|---|---|---|
| E1 | Invited address signs in through OIDC | principal created, invited role, invitation consumed (R3.1, R3.5) |
| E2 | Same verified address, password then OIDC | one principal, two credentials (R4.3) |
| E3 | OIDC asserts an unverified address | refused, nothing linked (R4.2) |
| E4 | Self-signup off, uninvited person tries every route | admitted nowhere (R4.4) |
| E5 | Self-signup on, address not invited | refused, attempt recorded (R3.3, R4.4) |
| E6 | Invite an address that is already a password account | adopted and re-roled, not duplicated (R3.2) |
| E7 | Self-service key request naming a scope | scope ignored, policy scope issued (R2.3) |
| E8 | Revoke, then use the key | refused on the next request (R2.5) |
| E9 | Device flow start to finish | key never appears on stdout or in any log (R2.2) |
| E10 | A new mount is added, floor tier asks for it | not visible until published (R5.3) |
| E11 | Floor tier asks for current data | delayed data only, predictions included (R5.2) |
| E12 | Uninvited CLI binds to the node | an explanation, not a stack trace (R5.4) |
| E13 | Agent attempts to grant `admin` unattended | refused regardless of earned trust (R6.4) |
| E14 | Sixty invitations from a list | volume breaker trips and asks (R6.3, ADR-045 D6.3) |

### The regressions

One test per failure of 2026-10-01, each of which fails before its fix:

- No prompt that reads a credential echoes it (R2.1), by a repository scan
  rather than a case list.
- `--help` imports no serving stack, measured lazy against eager in one
  environment.
- Every prompt the MCP server advertises can be fetched.
- A freshly scaffolded extension passes the linter that the scaffolder's own
  output tells the author to run.
- The documented install commands are the commands that work, executed from
  the guide.

## What R6 does not describe today

R6 says agents do the unattended work. On a fresh install they do not, and
four separate things stop them. Three are policy or configuration; the
fourth was a defect and is fixed.

- The secrets extension ships `[extension.mcp] enabled = false`, so an agent
  holds three read-only vault tools and cannot record an exposure or rotate
  anything.
- The `vault` agent is shipped and scheduled hourly but sits outside
  consent, so on at least one host it had not run in ten days.
- Its sweep could not classify what it found. `discover` decides `managed`
  from a fingerprint index, the heartbeat passes none and nothing populates
  one, so every finding fell to `unknown`, the unmanaged count was always
  zero, and the verdict was always "clean" — a check that could not fail.
  It now reports that it could not verify, which is the true answer.
  Making it classify instead means hashing credential values at store
  time, which is a custody decision rather than an implementation one, so
  this document does not claim the sweep classifies on a fresh install.
- The scanner could not recognise the platform's own credentials: the
  pattern for one provider's keys could not cross a hyphen, so neither of
  that provider's current key formats matched; the environment probe was
  written for one operating system on a fleet running another; and nothing
  read a shell startup file.

The first three are the gap between "an agent is shipped" and "an agent
runs", which is the same gap this whole document is about, one layer down.
Found by the session building credential hygiene.

## Out of scope

Self-service sign-up is designed and switched off; the public page is a
placeholder pending its own design; federated role claims are refused by
default pending an acceptance rule. Per-site role resolution is a named
requirement rather than a design: bundles resolve by role alone today, so a
role means the same thing at every site on a node, and at least one
extension is keying its own setting by site as a temporary seam until the
registry does it.
