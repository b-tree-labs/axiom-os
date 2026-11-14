# ADR-220: A storage provider is a connector type, built by a factory from configuration and mounted at runtime

**Status:** Accepted (2026-10-29; the owner approved the design and phase 1)
**Amends:** [ADR-062](adr-062-box-first-class-storage-connector.md) (its folding plan and v1 auth are replaced below), [ADR-110](adr-110-connector-identity-profiles.md) (the storage family's registry gains configuration-driven instantiation, extension discovery and reload), [ADR-112](adr-112-dry-versioned-artifact-substrate.md) (`infra.storage` implementations are obtained through this factory)
**Related:** ADR-057 (connector primitive), ADR-059 (one adapter per vendor), ADR-074 (registry fabric, `connection_ref`), ADR-023-A1 (portfolio gate on discovered code), ADR-182 / ADR-197 (never down from our own causes; the switch runner), ADR-194 (deposits are bronze), ADR-219 (bronze as immutable objects)

## Context

ADR-062 defined `StorageConnectorProvider` (LIST / READ / WRITE / WATCH /
REPLY_INGEST) and ADR-110 gave it a vendor-to-factory registry. Only `local` was
ever registered. Every other store grew its own path instead, and one vendor,
Box, is now woven into the architecture rather than plugged into it:

- its client, three auth flows (CCG, JWT, OAuth), source, provider and setup
  live in `data_platform/sources/box/`;
- generic machinery branches on its name: the forwarder's fallback target
  (`"box"` route), backup's off-site leg (`offbox == "box"`), partner health and
  function status (`current == "box"`), the connector skill
  (`GUIDED_KINDS = ("box",)`), the wizard (`_BoxHandler`), and the CLI
  (`--box-login`);
- three separate storage abstractions exist side by side (`connector/storage`,
  `infra.storage` with local and S3, and the publishing `StorageProvider` family
  with `box-browser` and OneDrive), and a fourth Box client sits in the signals
  document providers;
- deployments script directly against the vendor's REST API (token mint,
  folder listing, file download), because no Axiom verb reads "a configured
  store".

The cost is that a second vendor (S3, OneDrive, SharePoint, Google Drive) means
editing core modules, and a deployment cannot add or change a store without a
code change and a restart. The inventory below counts the touchpoints.

## Decision

**A storage provider is a connector type. Core code names a connector, never a
vendor. A connector's configuration says which `kind` it is; a factory
registered for that kind builds the instance; a reload mounts, replaces or
unmounts instances without restarting unrelated services.**

### 1. Registered by factory, never named by core code

- `connector/storage/registry.py` stays the one registry (ADR-110). A provider
  registers `kind -> factory(settings, *, secrets) -> StorageConnectorProvider`.
- Nothing outside a provider's own package names its kind. A guard test fails
  on a vendor string or a vendor module import outside the provider package
  (with a named, shrinking list of migration shims, the same discipline as the
  domain guard's debt list).
- `infra.storage` (ADR-112) and the publishing storage family keep their
  caller-facing interfaces as thin adapters over a registry instance, so there
  is one implementation per vendor (ADR-059).

### 2. Configuration-driven

A connector is declared once, as configuration, and everything that uses a
store refers to it by NAME:

```toml
# <state>/plinth/connectors/team-share.toml  (unchanged location and loader)
name = "team-share"
kind = "box"                         # selects the factory; the only place a vendor is named
credential_ref = "file://team-share-app"
[params]
folder_id = "123"
```

Callers take a connector name: the forwarder's fallback target
(`fallback_connector = "team-share"`), backup's off-site leg
(`offsite_connector = "team-share"`), a publishing target, a mirror source for
ingest, a deposit lane. The connector manager resolves the name to a live
provider. Existing `kind = "box"` connector files keep working unchanged; the
legacy backup keys (`offbox`, `box_folder_id`) and CLI flags are read through a
translator that ships inside the provider package and is removed in the
contract release.

### 3. Runtime mount and reload

A process-local **`StorageConnectorManager`** owns the live instances, keyed by
connector name, built lazily from configuration.

- **Triggers:** `axi connector reload [<name>]` (a skill function, ADR-056), a
  `SIGHUP` to a long-running service, or an opt-in configuration watch in
  service loops (off by default; event-driven where the platform has events).
  Reload also re-reads extension entry points, so a newly installed provider
  package is picked up.
- **Add:** the new connector is built and probed; it becomes available only when
  its probe passes.
- **Change:** a new instance is built **beside** the old one and probed; the swap
  is atomic and happens only on a passing probe (the switch-runner shape of
  ADR-197: prepare, probe, commit, rollback). On a failed build or probe, the old
  instance keeps serving and the connector reports `degraded` with the reason.
- **Remove:** the name stops resolving for new work; the instance closes when
  its last lease is released.
- **In-flight operations** hold a lease on the instance they started on and
  finish on it; a reload never interrupts a transfer. Long operations (a
  multi-part upload, a mirror pass) re-resolve the name at their next unit of
  work, not mid-unit.
- **Failure isolation:** one provider's build, probe or import failure affects
  only its connectors. A reload is per connector: a bad file never blocks the
  others, and nothing else restarts.
- **Health:** each connector reports `ready | degraded | failed | unmounted`
  with the last probe result and its declared capabilities, through
  `axi connector status` and the function status surface.

### 4. Providers may ship as extensions

A provider is discovered from the entry-point group `axiom.storage_providers`
(`kind = "pkg.module:register"`), or declared in an extension manifest:

```toml
[[storage_providers]]
kind = "s3"
factory = "axiom_ext_s3.provider:build"
capabilities = ["list", "read", "write", "conditional_put", "ranged_read"]
```

Loading is execution, so discovery honours the same portfolio gate as other
discovered code (ADR-023-A1 A1.3). A new vendor is a package, never a core
change. Built-in providers (`local`, `s3`, `box`) register the same way.

### 5. Capability negotiation

`StorageCapability` grows to cover what vendors differ on: `LIST`, `READ`,
`WRITE`, `WATCH`, `REPLY_INGEST`, plus `CHANGE_CURSOR` (incremental listing by
etag or cursor), `CHECKSUM` (a vendor content hash), `VERSIONING`,
`CONDITIONAL_PUT` and `RANGED_READ` (which ADR-219's object bronze needs),
`LARGE_UPLOAD` (chunked sessions) and `LOGIN` (an interactive or app-consent
login flow). A caller asks for what it needs; a missing capability is answered
before the call (`require(provider, {...})` raises naming the connector, the
kind and the gap), never by a vendor branch. **Auth is provider-internal:** a
provider declares its `auth_modes` (for Box: `ccg`, `jwt`, `oauth`,
`developer-token`), resolves its credential from `credential_ref`, and exposes
login and credential rotation through the `LOGIN` capability and the secrets
rotation registry, which it registers into.

## Options considered

- **Keep Box as a data-platform source kind and add others beside it.** The
  source-kind registry already selects by `kind`, but only for ingest. The
  forwarder, backup, publishing and status code would still branch per vendor,
  so every new vendor would edit them again.
- **Promote `infra.storage` (ADR-112) to be the one family.** Its interface is
  path-based upload and download for artifacts; it has no listing cursor,
  watch or capability set, and growing it into the connector protocol would
  duplicate ADR-062 under a different name.
- **Restart on configuration change.** Simplest, but a deployment would trade
  an outage of every service for adding a store, which ADR-182 rules out.

## Consequences

- One implementation per vendor, behind one protocol; a second vendor (S3,
  then OneDrive) proves it without touching callers.
- Deployments stop scripting against vendor REST APIs: they declare a connector
  and use the platform's verbs, which keeps a site repository to configuration.
- The guard makes a regression visible the day it is written.
- Costs: a mechanical move of the Box package (imports re-exported for one
  release), configuration keys renamed with a translator, and a manager that
  must be correct under concurrency (leases, atomic swap).

## Migration plan

| Phase | Scope | Behaviour change |
|---|---|---|
| **P0** | This ADR. | None. |
| **P1** | `StorageConnectorManager` (factory over configuration, leases, health, reload, `axi connector reload`); entry-point and manifest discovery; `BoxStorageProvider` implementing the protocol, with the existing client and auth flows moved under `connector/storage/box/`; `s3` provider wrapping `infra.storage.s3`; `local` unchanged; re-export shims at the old import paths. Existing Box tests pass through the new seam. | None. |
| **P2** | Callers switch to the manager and protocol: forwarder fallback target, backup off-site leg, partner health and function status, connector skill and wizard, publishing storage, signals document providers, preflight. Config translator for legacy keys; the vendor-string guard turns on with the shim list. | Config keys gain neutral names; old keys still read. |
| **P3** | First consumers: the mirror source and deposit lanes read through a storage provider, so a deposit-to-rows path names a connector, not a vendor; deployments replace direct vendor scripts with connector verbs. | Deployment configuration only. |
| **P4** | Contract: remove the shims, the legacy-key translator and the legacy flags; empty the guard's shim list. | Old keys stop being read (announced one release ahead). |

## Inventory (2026-10-29, Axiom `main`)

**Provider internals (move into the Box provider):** `data_platform/sources/box/`
(`api.py`, `session_api.py`, `ccg_auth.py`, `jwt_auth.py`, `oauth_auth.py`,
`provider.py`, `source.py`, `setup.py`); `data_platform/skills/box_login.py`
(becomes the provider's `LOGIN`); `scripts/box_oauth_login.py`;
`database/backup_uploader.py` `BoxBackupUploader` and `resolve_box_auth` (become
the provider's `put_file` with `LARGE_UPLOAD`); `forward/forwarder.py`
`BoxDropTarget` (becomes a generic storage drop target over `put_file`);
`publishing/providers/box.py`, `publishing/providers/storage/box_browser.py`
(a browser-session auth mode of the same provider);
`signals/extractors/docflow_providers` `BoxProvider` (URL resolution and fetch);
`secrets/foreign/rotation_providers.py` `box-oauth` (registered by the provider);
the Box half of `skills/verify.py` (becomes the provider's probe).

**Generic callers (switch to the manager and protocol):** `forward/forwarder.py`
routing (`"box"` route) and `skills/forward.py`; `skills/backup.py` and
`database/backup_policy.py` (`offbox`); `partner_health.py`,
`status/function_status.py`; `skills/connector.py` (`GUIDED_KINDS`, guided
setup); `connector/wizard.py` (`_BoxHandler` becomes provider-declared wizard
fields); `commands/cli.py` (`--box-login`, off-site secret flag);
`orchestration/box_run.py` (`run_box_to_rag` becomes `run_source_to_rag` over any
mirror source); `sources/__init__.py` re-exports; `publishing/cli.py`;
`infra/publication_registry.py` (`endpoint`); `skills/reindex.py`
(`DP1_BOX_SOURCE_NAME`); `infra/ratelimit.py` (a comment).

**Configuration (stays configuration):** connector files with `kind = "box"`;
the off-site connector name in the backup policy; `AXIOM_BOX_SESSION_DIR` and its
legacy alias in `infra/brand_migration.py`; the `boxsdk[jwt]` extra in
`pyproject.toml`.

**Outside this repository:** the domain consumer has no vendor code (its deposit
lanes read a `Deposit`, already provider-neutral). A deployment's site
repository holds direct vendor scripts (token minting, folder recon, file
download for several ingest jobs, the partner deposit pull and its receipts) that
P3 replaces with connector verbs, and its connector declarations, which stay.
