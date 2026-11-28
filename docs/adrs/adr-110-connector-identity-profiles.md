# ADR-110: Connector Identity Profiles — one model for a configured platform app, across auth-modes, tenancy, and clouds

**Status:** Proposed (2026-09-14)
**Deciders:** Benjamin Booth
**Related:** ADR-057 (connector as a top-level primitive), ADR-059 (connector-first
vendor unification — deferred the base-`Connector` generalization to a "future
ADR"; the cited ADR-061 number was later used for the agent-call protocol, so
that generalization was never settled — this ADR settles it), ADR-062 (storage
connector Protocol — defined but unimplemented), ADR-068 (connector detection
primitive), ADR-074 (registry fabric + `connection_ref` credential seam),
ADR-075 (SSO/OIDC delegated auth — the IdP registry), ADR-012 (provider
identity), ADR-103 (directory providers — the "nothing above the seam names the
vendor" invariant this extends to identity), ADR-052 (dependency direction:
scale providers behind a seam).

---

## Context

Axiom already integrates many clouds. Behind interfaces it ships **AWS, GCP,
Azure, and Google** adapters across five connector families — secrets
(`SecretStoreProvider`: AWS Secrets Manager, GCP Secret Manager, Azure Key
Vault, OpenBao, keychain), object storage (an S3 adapter that is AWS *or*
SeaweedFS by one `endpoint_url`), calendar (`CalendarProvider`: Microsoft,
Google, CalDAV), directory (`DirectoryProvider`: Entra, OIDC-claims, local), and
notification channels (Slack, Teams, Twilio, **AWS SNS**, **Azure
Communication**, **Google FCM**). ADR-059 decided *where* vendor adapters live
(the `connector/` extension, never implemented twice); ADR-074 decided the
*registry* that indexes them. The multi-cloud substrate is real.

Two decisions were nonetheless never made, and their absence is now felt
concretely.

**1. Five families each re-derive the same adapter shape.**
`SecretStoreProvider`, `StorageConnectorProvider`, `CalendarProvider`,
`DirectoryProvider`, and `ChannelAdapterProvider` are each "a `capabilities`
frozenset + a `kind`/`vendor` key + a name→builder registry + a factory",
written five times. ADR-059 §Decision.4 promised a base `Connector` Protocol
"so channel adapters, storage connectors, and calendar connectors all conform";
that ADR was never written. The cost is five places to teach, five subtly
different registries, and — until this session — one family (`auth`) that had
skipped the registry entirely and used closed `if/elif` dispatch, which is how a
Microsoft-shaped default (`_VENDOR_IDP.get(vendor, "entra")`) leaked into
generic code that would misroute a Google- or Okta-first deployment.

**2. "A configured platform app" is modeled three different ways, with no
first-class notion of its *type*.** A deployed Axiom holds several distinct
external app registrations at once. In one real institutional tenant today:

| App | What it is | auth-mode | tenancy | capability scope |
|---|---|---|---|---|
| A service assistant | unattended service identity | application / service | single-tenant | one storage site |
| A staff sign-in app | people signing *into* the product | user-signin (OIDC) | single-tenant | the product's own resources |
| A per-user automation app | a person automating *their own* cloud life | delegated-user | multi-tenant | the signed-in user's own data |

To an operator these three read as an incoherent zoo. They are in fact **three
points in one small space** — `(auth-mode × tenancy × capability-scope)` — that
the platform never names. In code the same three are scattered across three
unreconciled models: a token store keyed `(provider, user, scopes)`, a
`ConnectionInstance` in the registry fabric, and an older flat `Connection` /
`ConnectionRegistry`. None carries an explicit **auth-mode** field; service vs
delegated-user is only ever implied by which registry an object lives in or what
`owner`/`scopes` it happens to have. Because the type is never named, we also
cannot answer the portability question — "how does this same setup look on GCP
or AWS?" — with anything but prose.

**Consequence for current work.** The document mirror (PRESS) added this session
talks *straight to Microsoft Graph* — hardcoding MSAL, reinventing the OAuth
device flow — because ADR-062's `StorageConnectorProvider` Protocol, though
defined, has **zero implementations**, so there was no storage seam under it to
plug into. A scheduling consumer's calendar integration, meanwhile, needs a
*service* identity while the mirror needs a *delegated-user* one — the very
distinction the platform cannot yet represent.

## Decision

Adopt **Connector Identity Profiles**: a single first-class model for a
configured platform app, and the base Connector Protocol the families converge
on. Five parts.

### 1. One `Connector[Capability]` Protocol

The connector extension owns a single generic Protocol carrying provider
identity (ADR-012), a declared `capabilities` frozenset with registration-time
negotiation (an unsupported operation fails at wiring, never mid-run), and typed
resource references. The five families become **specializations** of it, not
five re-derivations. The `secrets` family — the most complete today (identity +
capability negotiation + typed `scheme://` refs + kind-registry, three clouds
live) — is the template. This settles the generalization ADR-059 deferred.

### 2. `ConnectionProfile` — the app-type model

A configured platform app/identity is **one record**:

```
ConnectionProfile:
  connector:        reverse-DNS vendor id (e.g. "com.microsoft.graph")
  auth_mode:        service | delegated_user | user_signin
  tenancy:          single | multi
  capability_scope: what the app may touch (site | mailbox | user-own | tenant)
  credential_ref:   a connection_ref into the credential seam (never inline)
  owner, status:    ADR-074 ConnectionInstance fields
```

`auth_mode` is the field that was always implicit; **making it explicit is the
crux of this ADR.** `ConnectionProfile` subsumes and replaces the three current
models; the token-store keyspace becomes the delegated-user profile's credential
detail, not a parallel registry.

### 3. Cross-cloud invariant

`ConnectionProfile` **names no cloud in its shape.** An Entra service app, a
Google service account, and an AWS IAM role are three `service` profiles; an
Entra delegated public client, a Google OAuth client, and an AWS Cognito user
pool are three `delegated_user` profiles. Nothing above the connector fabric
knows which cloud answered — the ADR-103/059 invariant, now extended from
capability to *identity*. "Would this work on GCP or AWS if we switched?" becomes
a fact about which profiles are registered, not a rewrite.

### 4. All credentials through the `connection_ref` seam (ADR-074)

Every ad-hoc credential key — `api_key_env`, S3 `access_key`, `DOCFLOW_CLIENT_ID`,
`GOOGLE_APPLICATION_CREDENTIALS`, on-disk `token_cache.json` — migrates behind a
`connection_ref`. A new cloud vendor is then added by registering **one
descriptor + one capability-scoped adapter + one or more profiles**, and nothing
else.

### 5. The document mirror becomes a registered connector kind (option B, decided 2026-09-14)

The mirror's remote side is a **versioned document editor** — read,
write-with-expected-version (optimistic concurrency, the never-write-blind
invariant), and versions — which the bulk file-store `StorageConnectorProvider`
(ADR-062) cannot express: its `put_file` is unconditional. Re-seating the mirror
onto it as-is would silently drop that invariant. So the versioned document
editor is its **own connector kind**, registered through the shared
`ConnectorRegistry` (§Decision-1). `GraphEditorEndpoint` is the built-in
`onedrive` editor, and the mirror resolves its endpoint via `get_editor(vendor)`
instead of a hardcoded class — so a Google Docs or S3-object-lock editor is one
`register_editor` call. The Graph transport is preserved *under* the editor
kind, not retired. The file-store `StorageConnectorProvider` stays a separate,
later concern for bulk operations (RAG ingest, artifacts) where conditional
single-document writes are not needed.

### 6. Mapping data is configuration, not code

The *mechanism* — the registry, the loader, the Protocol — stays in code. The
*data* — vendor→IdP maps, default scopes, endpoints, which connector answers for
a vendor — lives in **deployment-editable config files**, read at call time
through the config system's live file-watcher, so an operator changes a mapping,
adds a vendor, or points a vendor at a different IdP **in a live deployment with
no code patch and no restart**. Shipped defaults are data files (e.g. a packaged
`*.toml`), never Python literals; a `<config_dir>/*.toml` overrides or extends
them per entry. No `{"m365": "entra", …}`-style dict may live in a module.
_Proofs: the calendar vendor map (`vendor_defaults.toml` + a
`calendar-vendors.toml` override), and the document mirror's URL→editor
routing (`editor_routing.toml` + an `editor-routing.toml` override) — both
data files read at call time, no baked default in code._

## Consequences

**Positive.** One connector concept to teach instead of five. App-type becomes
legible: A/B/C stop being a zoo and become three `ConnectionProfile`s.
Cloud-portability becomes a registration fact. Credentials have one home. The
document-storage surface — today's sharpest Microsoft lock-in — joins the
generalized seam, and the mirror stops being a special case.

**Negative / risk.** This touches the platform spine (auth, storage, secrets,
calendar, directory). It is therefore **phased, TDD-first, and each phase ships
value** — never a big-bang restructuring (per the delivery-discipline
constraints). The convergence must not regress the mature families while lifting
the immature one.

**Neutral.** `ConnectionProfile` is a modeling consolidation, not new runtime
authority; existing tokens and grants keep working through their profile's
`credential_ref`.

## Implementation status

- **Phase 0 — LANDED (2026-09-14, branch `debt/auth-idp-registry`).** The `auth`
  family was moved onto the open IdP registry the other four families already
  use (`providers.get_idp` / `register_idp` / `available_idps`), and the two
  Microsoft-default leaks (`auth/consumes.py`, `schedule/calendar/connect.py`)
  were removed — an unknown provider/vendor now raises rather than silently
  resolving to Entra. This proves the target pattern on the family that was
  furthest from it, and is the first plank of the program below.

The remaining phases are specified in the accompanying goal and loop statements
(`docs/working/goal-connector-cohesion-2026-09-14.md`,
`docs/working/loop-connector-cohesion.md`).

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
