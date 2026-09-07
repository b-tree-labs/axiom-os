# axiom-ext-data-platform

**Data-platform backends for the Axiom platform** — Bronze/Silver/Gold persistence and query, designed so consumers (twin workflows, classroom analytics, federation receipts) can swap storage tiers without touching workflow code.

## What's in scope

| Tier | Phase | Backends |
|---|---|---|
| **Bronze** (raw, content-addressed receipts) | 6a (now) | `DuckDBBronzeReceiptStore` — single-node analytical SQL over a local file |
|  | 6a (now) | `MemoryBronzeReceiptStore` — in-memory; for tests + ephemeral pipelines |
|  | 6b (next) | `IcebergBronzeReceiptStore` — multi-writer, SeaweedFS-backed, prod volume |
| **Silver** (cleaned, joinable views) | 7 | DuckDB views over Iceberg |
| **Gold** (curated rollups) | 7 | Dagster-orchestrated transforms |

What's **not** in scope:
- The `BronzeReceiptStore` Protocol itself — that lives in `axiom.medallion.receipts` (axiom core), so this extension depends on axiom but no consumer extension depends on this.
- JSONL Bronze — stays in the twin extension as the zero-dep dev-time fallback.
- Cross-peer federated query — separate concern, separate extension when it lands.

## Install

```bash
pip install axiom-ext-data-platform               # base: protocol re-exports + memory store
pip install "axiom-ext-data-platform[duckdb]"     # + DuckDBBronzeReceiptStore
```

Backend modules guard their imports — calling a backend whose extra is missing raises a clean `ImportError` directing you to the right `pip install` line.

## Use

```python
from axiom_ext_data_platform import DuckDBBronzeReceiptStore

store = DuckDBBronzeReceiptStore(db_path="bronze.duckdb")
store.write_compute_receipt({"uri": "axiom://compute/sha256:...", "kernel": "openmc", ...})
found = store.lookup("axiom://compute/sha256:...")
```

The store satisfies `axiom.medallion.receipts.BronzeReceiptStore`, so any consumer that holds the Protocol type can swap it in transparently for `JsonlBronzeReceiptStore` (or any future backend).

## Why a separate extension

Per AEOS, storage backends are not core platform concerns. DuckDB, Iceberg, SeaweedFS, etc. each carry their own deps and ops surface; bundling them into axiom would force every consumer to install storage they don't use. Same shape as `axiom-ext-openmc`, where physics codes ship as an extension rather than core.
