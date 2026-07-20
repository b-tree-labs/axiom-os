# Build plan — tabular source lane (ADR-001, data_platform)

Delivers the tabular ingest lane from [ADR-001](decisions/adr-001-tabular-source-lane.md).
The lane shares the port above the sink and diverges at two seams (fetch unit,
sink), so the plan builds inward-out: contract → sink → kinds → promotion →
consumer cutover. Each phase is independently landable and independently tested.

**Sequencing note.** P0–P3 are Axiom-side, network-free, and land now. P4 is a
*consumer* cutover that lives in the consumer's site repo; it needs the consumer's
read-only source credential provisioned and the ingest host able to reach the
source — an operator/enabler step, tracked separately from the platform work.

---

## P0 — Contract seam (no behavior change)

**Lands in:** `data_platform/contracts.py`, `sources/contracts.py`.

- Add `shape: str = "document"` to `SourceKindProvider` (defaulted → every
  existing provider unchanged; registry protocol check still passes).
- Define `TabularIngestSource` (Protocol) + `RowBatch` (frozen dataclass), peers
  to `IngestSource` / `FetchedItem`, in `contracts.py`. Skeleton only — same
  posture as the existing abstract `SchemaPack` / `TransformPack`.
- Add `TabularBronzeSink` Protocol stub next to the existing sink protocol.

**Tests:** protocol shape / `runtime_checkable` acceptance; a document provider
with no `shape` still registers; a stub tabular provider registers and is
distinguishable by `shape`. Pure unit, no DB, no network.

**Gate:** ruff (repo config) + the data_platform unit suite green. No consumer
touched, nothing behaves differently. **Reviewable as a contracts-only PR.**

---

## P1 — Tabular bronze sink + row-level dedup

**Lands in:** `bronze/sinks.py`, `bronze/router.py`, `ingest_sink/core.py`.

- Implement `TabularBronzeSink`: land a `RowBatch` into a typed bronze table
  keyed `(source_name, natural_key, row_content_hash)`; content-address `raw`
  under the existing `_content/<sha256>` blob store.
- Wire the sink selection behind the `shape` switch in the `BronzeWriter` /
  `IngestSink` path — tabular batches route to `TabularBronzeSink`, document
  items keep routing to `FilesystemBronzeSink`. Disposition/classification/
  provenance gate is the **same** for both.
- Map the four-tier dedup ladder onto rows: connector etag → bronze
  `(source_uri, etag)` → row `content_hash` → typed table.

**Tests:** a fake in-memory `TabularIngestSource` yielding two batches with an
overlapping row proves row-level idempotency; re-landing an identical batch is a
no-op; a changed cell produces a new `content_hash` row. No network.

**Gate:** unit suite green; `IngestRunReport` records a tabular run with correct
landed/duplicate counts (run provenance works unchanged).

---

## P2 — Two built-in tabular kinds, with real preflight

**Lands in:** `sources/http_tabular/`, `sources/sql_tabular/` (+ registry
registration at import).

- `http-tabular`: fetch CSV/JSON over HTTPS → `RowBatch`. `preflight` = endpoint
  reachable + parses + sample row visible.
- `sql-tabular`: run a **declared read-only extract query** against a DSN (opened
  `read_only`, connection from `credential_ref`) → `RowBatch`. `preflight` =
  host reachable *from this host* + credential authenticates + query returns a
  sample row — each a `PreflightCheck` with `actor: admin` remediation
  (ADR-001 D6). The read-only open is defense-in-depth (D6).
- Credentials via `ConnectorConfig.credential_ref` resolved through the `secrets`
  extension (ADR-001 D5, secrets adr-003). Inline params still accepted for
  migration.
- `rest-api-tabular` (paged JSON) is a **stretch** in this phase — same shape,
  add only if a second API-shaped source is real.

**Tests:** parser unit tests (mirror the existing no-DB CSV-parse suite); a
preflight against a local fixture server asserts the ok / not-ok checklist
shape; a DSN-less `sql-tabular` preflight returns a blocker with `actor: admin`,
never raises. No live external DB in CI.

**Gate:** `axi data register <name> sql-tabular …` end-to-end (subprocess smoke,
per the CLI-smoke standard) producing a valid connector TOML + a preflight
report; `axi data list-kinds` shows both new kinds.

---

## P3 — Schema-map promotion + SCD-2

**Lands in:** `ingest_sink` / a new `promote/` module + a map format.

- A declared promotion map: bronze rows → silver/gold columns, optional
  extract/pivot expression (for EAV long tables), SCD-2 capture (`run_id`,
  `is_current`, `valid_from`/`valid_to`), and multi-source **precedence**.
- Map is **data** (TOML/SQL-template), loaded + validated at register time (a bad
  map is a preflight blocker, not a runtime crash).
- Promotion runs as a Dagster asset downstream of the tabular bronze landing —
  same orchestration the document lane uses.

**Tests:** a fixture bronze table + a map promotes to the expected gold rows;
SCD-2 correctly supersedes a prior `is_current` row on a changed value;
precedence resolves two sources writing the same key deterministically. No
network.

**Gate:** unit suite green; a promotion run appears in `RunStore` with row
lineage; `axi data` surfaces the promotion status.

---

## P4 — Consumer cutover (lands in the consumer's site repo)

**Lands in:** the consumer's site repo (not this extension) + a validation note.

- Register the consumer's tabular sources (`http-tabular` for the export,
  `sql-tabular` for the authoritative store) via `axi data register`, credential
  via `credential_ref`.
- Author the promotion map with source **precedence** (authoritative store >
  API > CSV export) and SCD-2 provenance, replacing the bespoke `PROMOTERS` dict.
- **Retire the bespoke site CDC script** — the hand-rolled hash table + promoters
  are now the platform's job.
- Dedup/rationalize the existing overlapping rows in the target gold table under
  the declared precedence.
- Arm the Dagster/timer schedule → **hands-free**.

**Enabler (tracked separately, not platform work):** the read-only source role
provisioned on the external store, its secret in the store's OpenBao, and the
ingest host's network path to the source confirmed (a `sql-tabular` preflight is
the check). Until that lands, P4 registers the source in a **dormant/preflight-
failing** state — visible and diagnosable, not silently broken.

**Done =** the consumer's stream is on an automatic CDC schedule through the
one port, the bespoke script is deleted, and the three overlapping paths are one
declared-precedence pipeline. This is the "hands-free + rationalized" bar.

---

## Dependency graph

```
P0 ──▶ P1 ──▶ P2 ──▶ P3 ──▶ P4
              (P2 needs secrets credential_ref; P4 needs the enabler above)
```

P0–P3 are one reviewable Axiom PR each (or P0+P1 bundled). P4 is a consumer-repo
PR gated on the enabler, and is where a bespoke ingest script finally retires.
