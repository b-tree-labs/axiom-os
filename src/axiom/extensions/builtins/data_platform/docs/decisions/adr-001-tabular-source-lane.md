# ADR-001 (data_platform): A tabular source lane, peer to the document/RAG lane

**Status:** Proposed · **Date:** 2026-07-16
**Extension:** `data_platform`
**Builds on:** the `SourceKindProvider` / `IngestSource` / `FetchedItem` contracts (`sources/contracts.py`, `contracts.py`), the connector registry (`agents/plinth/connectors.py`), `RunStore`/`IngestRunReport` (`ingest_run/`), the bronze writer + provenance/disposition gate (`bronze/`), platform ADR-093 (DB topology + `session_for("data_platform")`), secrets adr-003 (OpenBao-default SecretStore + credential-ref).
**Locks:** the `shape` dimension of a source kind (`document` | `tabular`).

## Context

The extension already ships a **mature ingestion port** — but for exactly one
shape of data. A source registers as a `SourceKindProvider` (`register <name>
<kind>`), the platform drives it kind-agnostically through
`IngestSource.list_changed()` → `fetch()`, each item comes back as a
`FetchedItem` carrying **opaque `content: bytes`**, the `BronzeWriter`
provenance/disposition gate lands it, and the terminal sink chunks-and-embeds it
into a RAG corpus (`ingest_sink` → `rag_embed` → pgvector). Around that core sit
capabilities every source inherits for free: register-time **preflight** with
non-coder remediation (`PreflightResult`/`PreflightCheck`, `actor: you|admin`),
a four-tier dedup/CDC ladder (connector etag → bronze `(uri, etag)` → silver
`content_hash` → RAG `(source_path, checksum)`), `RunStore` provenance, the
**PLINTH** observe→diagnose→remediate agent, Dagster scheduling, and per-source
`default_disposition` / `default_tier` classification + federation gating.

That last cluster is precisely the multi-org governance the platform will need as
"many organizations supply data through one port." The port is real, it is good,
and it is **document-shaped end to end**: the natural unit is a byte blob, the
terminal artifact is a retrievable chunk. Its roadmap kinds (GDrive, SharePoint,
S3, GitHub, JIRA, Confluence, local FS) are all document sources.

**Structured, tabular, time-series data does not fit this shape**, and a real
consumer already exposed the gap. Its data is not documents-to-retrieve; it is
**rows with a schema, promoted to typed tables that SQL verbs read**. The
consumer needed to ingest the *same* series from three angles — a periodic **CSV
export over HTTPS**, the authoritative **external OLTP store** (an EAV/long table
with SCD-2 run-versioning, reachable only across a narrow network boundary: one
port from one host), and a **BI-tool REST API** — and land them in one gold
table with source precedence. Because the platform has no tabular lane, that
consumer hand-rolled a **bespoke site script**: its own hash-keyed CDC table, its
own hand-coded promoters, and — the real cost — **none** of the port's preflight,
credential custody, classification, federation, PLINTH, or run provenance. When
the narrow network boundary bit (wrong host, blocked port, wrong credential), it
surfaced not as a preflight checklist at register time but as days of manual
archaeology.

The gap is not plumbing — the registry, preflight, connectors, CDC ladder,
run store, and PLINTH are all already shape-agnostic. The gap is **one shape**:
the source's item unit and the terminal sink.

## Decision

Add a **tabular lane** as a first-class peer to the document lane. It shares
every layer of the port *above* the sink and diverges only at two seams: the
source's fetch unit, and the sink. A source kind now declares which shape it is.

### D1 — A source kind declares its `shape`

A source kind may declare an optional attribute:

```python
shape: str = "document"      # "document" | "tabular"
```

It is **not** a required `SourceKindProvider` member — on Python 3.12+ a
`runtime_checkable` Protocol checks data members via `hasattr`, so making it
required would break the registration `isinstance` check for every existing
provider. Instead the platform reads it via `source_shape(provider)` →
`getattr(provider, "shape", "document")`, the same optional-capability idiom as
`SupportsUrlFor` (ADR-091). Existing providers need no change and still register;
the in-tree document provider sets `shape = "document"` explicitly for clarity.
The registry, `axi data register`, `ConnectorConfig`, preflight, `RunStore`, and
PLINTH are **untouched** — they are already shape-agnostic. The CLI dispatcher
reads `shape` only to pick which runtime protocol `construct()` must return and
which sink the run uses.

### D2 — Tabular sources fetch typed rows, not bytes

A sibling runtime protocol, peer to `IngestSource`, keeps the same CDC surface
and swaps the fetch unit:

```python
@runtime_checkable
class TabularIngestSource(Protocol):
    name: str
    schema_ref: str                          # the declared column contract this source fills
    def list_changed(self, since: datetime | None = None) -> list[str]: ...  # same watermark/etag CDC
    def fetch_rows(self, item: str) -> RowBatch: ...

@dataclass(frozen=True)
class RowBatch:
    source_name: str
    item_id: str                             # CDC key, as FetchedItem.item_id
    etag: str | None                         # CDC key, as FetchedItem.etag
    modified_at: datetime | None             # watermark, as FetchedItem.modified_at
    schema_ref: str                          # which declared schema these rows satisfy
    rows: list[dict[str, object]]            # typed cells (Arrow table permitted later)
    raw: bytes                               # the exact fetched payload — content-addressed for replay/audit
    extra: dict[str, str] = field(default_factory=dict)
```

`list_changed` / `item_id` / `etag` / `modified_at` are deliberately identical to
`IngestSource` so the **existing watermark + etag CDC drives both lanes
unchanged**. `raw` preserves the byte payload so bronze stays reproducible and
auditable exactly as the document lane's content blob does.

### D3 — A tabular bronze sink lands rows in a typed table, with row-level dedup

`TabularBronzeSink` is a peer to `FilesystemBronzeSink` behind the same
`BronzeWriter` gate (so disposition/classification/provenance are enforced
identically). It lands each `RowBatch` into a typed bronze table keyed
`(source_name, natural_key, row_content_hash)` and content-addresses `raw` under
the same `_content/<sha256>` blob store. The four-tier dedup ladder maps
straight across: connector etag → bronze `(source_uri, etag)` → **row
`content_hash`** (the silver-tier hash, applied per row) → typed table. Nothing
about the ladder is document-specific; only the unit shrinks from a file to a
row.

### D4 — Promotion is a declared schema-map + SCD-2 provenance, not embedding

Where the document lane's terminal step is chunk-and-embed, the tabular lane's is
a **declared promotion**: a column map (bronze rows → silver/gold columns), an
optional extract/pivot expression for EAV-shaped sources, an SCD-2 provenance
capture (`run_id`, `is_current`, `valid_from` / `valid_to`), and — when several
sources feed one target — a **precedence** ordering. The map is **data, not
code**: a new tabular source is a registration plus a map file, not a bespoke
promoter. This is the direct antidote to the hand-coded `PROMOTERS` dict the
bespoke script carries.

### D5 — Credentials move to a secret-ref, for both lanes

`ConnectorConfig` gains `credential_ref: str | None` — a handle the `secrets`
extension resolves (OpenBao by default, per secrets adr-003) at construct/
preflight time. This replaces inline credential blobs (e.g. the document lane's
`session_state_b64`) and gives the tabular lane its DSN/token without a secret
ever touching the connector TOML, git, or a process argv. The platform still
never reads `params`; it resolves `credential_ref` and hands the plaintext only
to the kind's provider.

### D6 — Reachability + credential validity are first-class preflight checks

A tabular kind's `preflight()` opens its connection **read-only**, confirms the
target is reachable *from this host*, the credential authenticates, and a sample
row is visible — each an ordinary `PreflightCheck` with `actor: admin`
remediation when it fails. A narrow network boundary or a wrong credential then
surfaces as one red checklist line **at register time**, not as a silent
crashloop discovered days later. (The read-only open is also defense-in-depth: a
bug in the platform can never write a consumer's system of record.)

## The stress test (anonymized worked example)

One consumer, one series, five source shapes — every one lands on the unified
contract. Shapes D and E already work today; A–C are what this ADR adds.

| Source shape | Kind | Fetch unit | Sink | New machinery |
|---|---|---|---|---|
| **A.** CSV export over HTTPS | `http-tabular` | `RowBatch` from parsed CSV | typed bronze table → gold via map | D2, D3 |
| **B.** External EAV/SCD-2 OLTP store, one port from one host | `sql-tabular` | `RowBatch` from a declared read-only extract query (pivots the EAV long rows) | same table, precedence over A | D2–D6 (reachability preflight + read-only + credential-ref carry the whole weight of the "narrow boundary" pain) |
| **C.** BI-tool REST API over the same series | `rest-api-tabular` | `RowBatch` from a paged JSON response | same table, lowest precedence | D2, D3, D5 |
| **D.** Object-store document API | `box` (exists) | `FetchedItem` bytes | RAG corpus | — |
| **E.** Local files | `fs` (roadmap) | `FetchedItem` bytes | RAG corpus | — |

The three overlapping paths into one gold table (A, B, C) stop being a `max()`
hack in a bespoke script and become a **declared precedence** on one promotion
map (B > C > A). The "reachable only across a narrow boundary" property — the
single hardest thing about this consumer — becomes exactly one `PreflightCheck`.
SCD-2 run-versioning (backfill reruns vs contemporaneous rows, the provenance
seam) is captured by D4's provenance columns instead of ad-hoc source columns.

## Consequences

- **One port, two shapes.** A source is document *or* tabular; everything from
  register through preflight, CDC, run provenance, classification, federation,
  PLINTH, and Dagster is one code path. Adding a third shape later (event stream)
  is a new `shape` value + a sink, not a parallel platform.
- **Multi-org governance applies to tabular for free.** `default_disposition` /
  `default_tier` classification and federation gating — the reason the port suits
  "many organizations supplying data" — now cover structured feeds too. The
  bespoke script had none of it.
- **Bespoke site ingest scripts retire.** A consumer's hand-rolled tabular CDC
  becomes `axi data register` + a map file. The site stops carrying platform
  code it has to maintain (aligns with "minimal human steps" and "errors →
  product guardrails").
- **Reachability failures shift left.** The narrow-boundary / wrong-credential
  class of failure moves from days-of-archaeology to a register-time checklist.
- **Back-compatible.** `shape` defaults to `document`; no existing provider,
  connector TOML, or test changes. `credential_ref` is additive/optional; inline
  params still work during migration.
- **Cost.** A second bronze sink, a `RowBatch`/`TabularIngestSource` protocol
  pair, a promotion-map format, and SCD-2 columns — plus the discipline that the
  map stays *data*. Modest, and mostly new surface rather than churn on the
  document lane.

## Alternatives considered

- **Harden the bespoke site script.** Rejected: it permanently forks tabular
  ingest away from the port, re-implements governance the platform already has,
  and multiplies per-consumer as more organizations supply data — the opposite
  of "one well-documented port."
- **Force tabular data through the document lane** (serialize rows to a blob,
  chunk, embed). Rejected: the terminal artifact is wrong (chunks, not a queryable
  table), SQL verbs can't consume it, and SCD-2 provenance is lost.
- **A separate `tabular_platform` extension.** Rejected: it would duplicate
  registry, preflight, connectors, run store, and PLINTH. The whole point is that
  those are already shape-agnostic; the divergence is two seams, not a platform.

## Rollout

Phased plan in `docs/tabular-source-lane-build-plan.md`. P0–P3 are Axiom-side and
land independently; P4 is the consumer cutover that retires the bespoke script.
