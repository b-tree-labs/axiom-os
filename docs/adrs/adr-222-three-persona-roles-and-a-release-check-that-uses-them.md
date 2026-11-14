# ADR-222: Three persona roles, and a release check that uses them

**Status:** Proposed (2026-10-30)
**Role renamed by [ADR-261](adr-261-the-governing-role-is-site-admin-and-admin-reads-as-access-admin.md) (2026-11-12):** the role `operator` is `site-admin` ("Site admin"), and `admin` is shown as "Access admin". Rights unchanged; a grant stored as `operator` still resolves.
**Builds on:** [ADR-151](adr-151-roles-separate-governing-from-reading.md) (the role vocabulary, and
governing is not reading), [ADR-153](adr-153-a-role-is-granted-at-a-site.md) (a role is granted at
a site), [ADR-207](adr-207-one-serving-mode-for-every-surface-a-bound-node-forwards-shared-reads.md) (one serving mode; rule 7, a client and its node
compare versions)

## Context

ADR-151 gave the gate six roles cut by the *kind* of act: reading, writing, governing. That cut is
right for the audit record and for an administrator who must not read. It is the wrong cut for the
first hour of a new colleague, who arrives with a job, not with a theory of acts.

On 2026-10-26 the first colleague signed in, made a self-service key and every data read answered
403. The role they were given was `viewer` (`*:read`), and a data question arrives as POST, which
the gate reads as `invoke`. One declaration fixed that (`MountSpec.reads`, so the data door's query
path is a read). The same role also could not chat: a chat completion is a POST to the gateway, and
no read-only grant will ever cover it. Nobody had asked the role to do what the person holding it
came to do, so nothing noticed that it could not.

There are three people in the first hour, and each is the one before plus one thing:

- somebody who reads the site's data and documents and asks chat about them;
- somebody who also brings their own data as a kit, or files documents, for review;
- somebody who also runs the node: reviews and approves what was submitted, grants roles.

## Decision

### 1. Three persona roles, each a superset of the one before

| Role | Reaches | Scopes |
|---|---|---|
| `reader` | data, documents, chat | `*:read`, `gateway:invoke`, `chat:invoke` |
| `contributor` | the reader's, plus kit submission and document upload | the reader's, plus `data_platform:invoke`, `library:invoke` |
| `operator` | the contributor's, plus governance | ADR-151's `operator`, unchanged: `*:read`, `*:invoke`, `*:govern` |

They are registered beside ADR-151's six, not instead of them. `operator` is the same role in both
lists; the ladder asserts only that it holds everything `contributor` holds.

Chat is named by mount (`gateway` serves `/v1/chat/completions`, `chat` the agent loop) rather than
granted as `*:invoke`, because `*:invoke` is every write on the node and a reader writes nothing.
A submission is likewise named by mount. The scope grammar is per mount, so `data_platform:invoke`
also covers the intake's `POST /ingest`; a deployment that wants contributors to submit kits and
never push readings narrows `contributor` in its roles file, which may only narrow.

### 2. The first sign-in is a reader

An SSO account created on first sign-in with no roles claim receives `reader`
(`AXIOM_GATE_JIT_ROLE`, default changed from `viewer`). Its self-service key therefore holds the
reader's scopes. An explicitly empty variable still opts out. Self-signup (off by default, behind
`AXIOM_GATE_SELF_SIGNUP`) still creates an account with no role, because it admits people nobody
has identified; granting it a floor is a separate decision.

### 3. Governance through the data door has a declared route

ADR-151 §2 left each extension to declare which of its routes govern. The data door now has one:
`POST /data/review`, the kit review queue (bundles waiting for approval, and withdrawal requests),
declared in `governs`. Served, it is held to the credential's site like every other kit read.
Approving stays on the node, as it was.

### 4. A release check mints a key per role and uses it

`python -m axiom.extensions.builtins.webgate.role_check` signs a real session for an account
holding each persona role, mints a real self-service key through `POST /gate/keys`, and presents
it to the composed app's real authz middleware, with the shipped mount declarations and stub
handlers. It checks five representative acts (a data read, a document search and listing, a chat
completion, a kit submission, the review queue) against the table above, in both directions: a
role that cannot reach what it should fails, and so does a role that reaches what it must not.

The publish workflow runs it before a wheel is published. The same module checks a running node
with `--node`, one `--role` at a time, signing in to mint the key there or presenting one already
minted, which is the hook a consumer's onboarding job calls.

### 5. The node says which client it needs

`GET /api/v1/version` advertises `min_client` (default `0.70.9`, the first release whose kit
submission uses `/data/submit`) and `capabilities`, read off the routes the node mounted. A client
asks once a day per binding, and when it is older says so once, in words, with the one command
that updates it, instead of leaving the person to decode a 403. It never blocks and never raises,
and a node that does not answer changes nothing.

## Consequences

**A first sign-in can chat**, which also means a first sign-in costs model time. That was always
the intent of letting people in; the serving budgets (ADR-157) are what bound it.

**`viewer` no longer is what most people hold.** It keeps its meaning (read-only on every mount),
and an account already holding it keeps it. Moving such an account to `reader` is an
administrator's one command (`gate role --add reader`), not a migration.

**A deployment roles file that defined `reader` or `contributor` itself is now an override of a
built-in, so it may only narrow.** A file that granted either role more than this ADR does (for
example `contributor = *:invoke`) is refused at start-up with the scope named. Before this ADR
any new role inside the registered union was accepted. Check a node's roles file before upgrading
it; a wider grant belongs under a different name.

**The check proves scopes, not engines.** In process, the authz engine is permissive and only the
key's scopes decide, which is the hook's contract (scope enforcement runs first and no engine can
widen it). Run against a live node, the node's own engine decides too.

**A new act a persona needs is a new probe.** When a persona gains a job (say, saving a chart to a
shared catalogue), the probe goes into `role_check.PROBES` and the expectation into `REACHES` in
the same change, or the role can quietly lack it again.
