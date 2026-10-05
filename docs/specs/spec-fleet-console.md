# Tech Spec: Fleet Console (`fleet` extension)

**Status:** Living — updated with each behavior-changing PR.
**PRD:** `docs/prds/prd-fleet-console.md`. **ADR:** ADR-119.

## 1. Shape

`src/axiom/extensions/builtins/fleet/` — AEOS-conformant, scaffolded on
the `graduation` layout. Surfaces:

| Surface | What |
|---|---|
| api kind | `subpath = "/fleet"` → `POST /api/v1/fleet/reports`, `GET /api/v1/fleet/status`, `GET/POST /api/v1/fleet/pins` |
| cmd noun `fleet` | `axi fleet status`, `axi fleet report`, `axi fleet pins [declare]`, `axi fleet enroll` — thin wrappers over skills (ADR-056) |
| skills | `fleet.status`, `fleet.report`, `fleet.pins`, `fleet.pins_declare`, `fleet.enroll` — `(params, ctx) -> SkillResult` |
| MCP | read-only `fleet.status` projection (write verbs stay off MCP pending write policy, as schedule does) |

## 2. Storage (`session_for("fleet")`, Alembic per schedule pattern)

- `fleet_nodes` — `node_id (pk), site, display_name, profile, enrolled_at,
  archived_at, cadences JSON ({kind: seconds})`. Row created on first
  accepted report or explicit enroll; site always from credential.
- `fleet_reports` — append-only: `id, node_id, site, kind, payload JSON,
  received_at, reporter_principal, envelope_hash, signature_state
  (unverified|verified|failed)`. Bounded by retention (per-kind cap +
  age); pruning is a skill with a receipt.
- `fleet_latest` — projection maintained transactionally on ingest:
  `(node_id, kind) pk, report_id, received_at`.
- `fleet_pins` — `id, site, scope (platform|site-repo|package name),
  channel, declared_version, source, declared_by, declared_at,
  superseded_at`. Declarations are append-only; current = latest
  non-superseded per (site, scope, channel).

No hardcoded `schema=`; store module exposes `session_scope()` over a
swappable provider so unit tests bind SQLite (schedule's pattern).

## 3. Ingestion

`POST /api/v1/fleet/reports` — body is a Digest (`federation/digest.py`)
whose findings carry report payloads; per ADR-119 phase 1 the transport
bearer credential is authoritative:

1. Resolve principal via the standard bearer resolver; no principal →
   401 (fail closed). Site = principal context (never payload).
2. Validate kind ∈ v1 set; unknown kind → 422.
3. Size caps per the ingest-sink convention (`AXIOM_FLEET_MAX_*` envs).
4. Envelope hash recorded; signature verification phase 2 → until then
   `signature_state = "unverified"` is stored and *surfaced* (an
   unverified report can render at best UNPROVEN-verified, keeping the
   gap visible rather than silent).
5. Insert report + upsert `fleet_latest` + upsert `fleet_nodes` in one
   transaction; caller gets `{accepted, report_ids, receipt}`.

## 4. Report kinds and their effect-evidence bindings

| kind | producer (existing) | GREEN requires (observed effect) | else |
|---|---|---|---|
| `heartbeat` | node cron/PULSE tick | fresh within 3× cadence (freshness IS the effect) | STALE |
| `service_health` | `status/cli.py::HealthChecker.check_all()` | every service `healthy`, each with `latency_ms` present | worst-wins per that module's rule |
| `backup` | `data_platform/skills/backup.py` result | `size_bytes > 0` and `artifact` present and `created_at` fresh vs backup cadence | FAILED if error; UNPROVEN if fields missing |
| `backup_validate` | `skills/backup_validate.py` result | all five checks PASS incl. `restore_live` | WARN→UNPROVEN, FAIL→FAILED (split preserved verbatim) |
| `canary` | `CanaryAttestation.to_dict()` | `status == "green"` with non-empty `smoke_results` | red/rollback → FAILED; empty smoke → UNPROVEN |
| `versions` | `axi --version` + site pin file read on node | payload lists `{package: version}`; GREEN is not computed here — drift view consumes it | missing → node shows VERSION-UNKNOWN |

Status enum: `GREEN | UNPROVEN | STALE | FAILED | UNKNOWN` with rollup =
worst-wins (`FAILED > STALE > UNPROVEN > UNKNOWN > GREEN`). Each GREEN
predicate has a can-fail test (negative fixture) — enforced by the test
suite's structure, one test class per kind.

## 5. Staleness

`stale_after(kind, node) = 3 × cadences[kind]` (node-declared at enroll;
default 3× 15 min for heartbeat, 3× daily for backup kinds). Evaluated at
read time from `fleet_latest.received_at` — no background marker job, so
the judgment cannot itself go stale.

## 6. Drift (release channels)

`drift(site) =` for each current pin `(scope, channel, declared_version)`
× each non-archived node of the site: compare against the node's latest
`versions` payload → `in_sync | behind | ahead | unreported`. Rendered in
`fleet status` and `fleet pins`. `VersionDirective` records (min-version
directives) join as a second declared source, labeled.

## 7. Node-side push (`fleet.report` skill)

Gathers a snapshot locally — `HealthChecker.check_all()`, latest backup /
backup-validate skill results (from their receipts/paths), canary
attestation file if present, versions — builds the Digest, POSTs to the
configured target with the node's credential. Config lives in the node's
settings (`fleet.push_url`, `fleet.credential_ref` via secrets); absence
= not opted in, skill says so and exits 0 (probe rule: never warn inside
a probe). Scheduling rides PULSE (a `ScheduleDefinition` registered at
enroll; outcomes land in `ScheduleFireLog` as usual).

## 8. Tenancy & auth

Serving: `site_scope.resolve()` ∩ principal grant, 404-not-403
(`SiteOutOfScope`). Ingest: bearer per-node key minted via
`webauth/api_keys.py` with `bind_principal_to_site`; the ingest grant
check mirrors `ingest_sink/tenancy.py` (site from credential, never
payload). Mount posture: contribute to `/api/v1` via
`webapp/api/contributions.py`; per-route auth dependencies as chat does.

## 9. Phasing

- **P1 (this branch):** store + ingestion route + `fleet.report` +
  `fleet.status` with staleness + effect bindings for `heartbeat`,
  `service_health`, `backup`, `backup_validate`; CLI verbs; standard
  tests. Ships value: our tenancy pushes and reads real status.
- **P2:** Ed25519 envelope verification (`verify_digest`), canary kind
  wired to an HTTP sink (spec-canary-nodes §4), pins + drift view,
  MCP read tool.
- **P3:** web tier rendering via `/api/v1`, retention/archival skills,
  multi-site rollups.
