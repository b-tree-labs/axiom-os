# `data_platform` — Axiom Data Infrastructure Extension

> The Axiom data infrastructure extension: a federation-aware,
> classification-gated, medallion-architected ingest + lakehouse +
> embedding pipeline. Connects any external source to a queryable
> RAG corpus with audit-grade provenance from byte to chunk.

## What this is

A production-grade ingest + storage + embedding stack delivered as
an Axiom extension. The "what" is simple:

- **Ingest** from external sources (Box today; GitHub, GDrive, S3,
  SharePoint, file system in roadmap) into a bronze tier with
  provenance gating.
- **Materialize** through a medallion architecture (bronze → silver
  → gold) via Dagster orchestration with Iceberg + dbt + DuckDB.
- **Index** into pgvector for RAG retrieval with classification +
  citation.
- **Verify** end-to-end with a built-in eval harness measuring
  with-vs-without-RAG quality.

## Architecture

```
                                                                       
   Source         Connector         Bronze       Silver/Gold      RAG  
   ──────         ─────────         ──────       ──────────       ───  
   Box folder ─▶ BoxIngestSource ─▶ Bronze   ─▶  Iceberg     ─▶ pgvector
                  (catalog +         (raw +      medallion       (chunks
                   etag-skip +        provenance  (dbt +          + cite
                   JWT auth +         gate +      DuckDB +        chain)
                   rate-limit         classify)   classify        
                   helper)                        re-screen)      
                                                                       
                                                                       
                  ▲          ▲          ▲           ▲              ▲   
                  │          │          │           │              │   
                  │       PLINTH ───── observes + diagnoses + remediates 
                  │       agent       (ingest-fix-ingest loop)         
                  │                                                    
                  │                                                    
            ConnectorCursor      ConnectorCursor handles the
            (etag persistence,    "what did I already process"
             watermark, resume)   question across runs.                
```

### The dedup tiers

Four invalidation keys at four points (see
`feedback_rag_dedup_three_tiers` memory):

| Tier | Key | Skips |
|---|---|---|
| Connector | `etag` (or `modified_at`) | already-downloaded source files |
| Bronze | `(source_uri, etag)` | already-landed bronze rows |
| Silver | `content_hash` | same content under different paths |
| RAG | `(source_path, checksum)` | re-indexes when content unchanged |

## CLI surface

The extension contributes verbs to the `axi` CLI. All take
``--corpus rag-{community,org,internal}`` where applicable.

| Command | Purpose |
|---|---|
| `axi rag add <file>` | One-shot single-file ingest with optional `--source-path` override. |
| `axi rag remove <name>` | One-shot removal by basename or full path; dry-run by default. |
| `axi rag audit --rules <toml>` | Audit corpus against provenance rules; `--purge` to act. |
| `axi rag search <query>` | Hybrid vector + text search; classification-respecting. |
| `axi rag eval --questions <yaml>` | Run RAG-vs-baseline benchmark; prints `lift` metric. |
| `axi rag status` | Per-corpus chunk + document counts. |

(There's also a planned `axi lakehouse` noun for the infra side and
`axi data` noun for the user-facing tap — see `feedback_axi_data_is_misnamed`.)

## Box connector setup

The first concrete connector. Two auth modes:

### Production: JWT (Server Authentication)

Mints + auto-refreshes 60-min tokens forever. Zero ongoing operator
input after a 10-min one-time setup.

1. https://app.box.com/developers/console → **Create Platform App**
2. **Custom App** → **Server Authentication (with JWT)**
3. Configuration tab:
   - **App Access Level:** "App + Enterprise Access" (or "App Access Only"
     + share target folders with the service principal)
   - **Application Scopes:** check "Read all files and folders stored in Box"
   - **Advanced Features:** "Generate user access tokens"
4. **Add and Manage Public Keys** → **Generate a Public/Private Keypair**
   → downloads `<config>.json`
5. Enterprise admin: **Admin Console → Custom Applications → Add**
   → paste the Client ID → **Authorize**
6. Store the JSON as a k8s Secret + reference from chart values:
   ```bash
   kubectl create secret generic box-jwt-config \
     --from-file=config.json=./<config>.json -n axiom-data
   ```
7. Reference in helm values:
   ```yaml
   connector:
     box:
       jwtConfigSecret: box-jwt-config
   ```

The daemon picks up `BOX_JWT_CONFIG`, mints tokens, refreshes when
within 5 min of expiry. Operator is done.

### Bootstrap / dev: Developer Token (60 min)

For local dev or while JWT is being authorized. Generate a 60-min
token from the developer console; set as env:

```bash
export BOX_DEVELOPER_TOKEN=<token>
```

## Helm install

```bash
helm install axiom-data-platform ./helm \
  --namespace axiom-data \
  --create-namespace \
  --set dagster.axiomVersion=<latest> \
  --set connector.box.folderId=<your-folder-id> \
  --set connector.box.jwtConfigSecret=box-jwt-config
```

The chart bakes in (was: manually patched every install pre-v0.30.5):

- Workspace flag (`-w /opt/dagster/dagster_home/workspace.yaml`)
- `AXIOM_RAG_OCR_ENABLED=1` env on daemon
- OCR + extraction Python deps installed in initContainer:
  `pypdf pypdfium2 pytesseract python-docx openpyxl requests`
- System binaries installed via apt: `tesseract-ocr poppler-utils`
- Dagster I/O storage emptyDir mount (writable materialization path)

## Eval harness

Validate that retrieval actually helps. Standard nuclear-engineering
question set bundled at `docs/working/rag-eval-nuclear-v0.yaml`.

```bash
# Full comparison: baseline vs with-RAG, prints lift
axi rag eval --questions docs/working/rag-eval-nuclear-v0.yaml

# Baseline only
axi rag eval --questions <yaml> --no-retrieval

# Quick smoke (3 questions)
axi rag eval --questions <yaml> --limit 3
```

Scoring (v0):

- `score_substring` — case-insensitive phrase match against
  `expected_answer_contains`
- `score_citation_overlap` — Jaccard over retrieved citations vs
  `expected_citations`

v1 (planned): LLM-judge faithfulness, ROUGE content, hallucination
detection.

## OCR fork

Scanned PDFs (e.g. CRISP Literature archive) silently dropped from
the corpus when native `pdftotext + pypdf` extraction returns near-
zero chars. The OCR fork (`axiom.rag.ocr`) catches that:

```
extract_text(pdf)
  ↓
native extract (pdftotext → pypdf)
  ↓ (if < 100 chars)
extract_pdf_with_ocr_fallback
  ↓
TesseractEngine (pypdfium2 raster + pytesseract OCR)
  ↓
OcrResult(text, engine, page_count, confidence)
```

Provenance survives into `FetchedItem.extra` via `ocr_provenance_extra`
so silver-tier de-quarantine can read engine + confidence.

Opt-in via `AXIOM_RAG_OCR_ENABLED=1` env (set automatically by the
chart). Required apt packages: `tesseract-ocr poppler-utils`. Required
Python: `pypdfium2 pytesseract`.

## Connector hardening

Tonight's stand-up surfaced ~10 failure patterns the connector now
handles:

- **Rate-limit aware:** `RateLimitWindow` parses every response's
  `X-RateLimit-*` + `Retry-After`; threads share state.
- **Typed 429:** `RateLimited(window)` exception (PLINTH skills
  pattern-match the type).
- **etag skip:** `If-None-Match` on `get_json` / `get_bytes`;
  304 returns `None`.
- **catalog/fetch split:** `BoxIngestSource.catalog()` returns
  `list[ItemMetadata]` — caller dedups/routes/budgets before paying
  byte tokens.
- **Resumable:** `ConnectorCursor` persists `(seen_etags, watermark)`
  atomically; runs resume across token cliffs.

See PR #416, #441 for chart bake-in, #442 for JWT auth.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Sensor SKIPPED + DEADLINE_EXCEEDED | Sensor evaluation > 60s on large corpus | Launch run manually via GraphQL; sensor is for *detection*, not bulk work. PLINTH skill `run launch manual`. |
| Run SUCCESS but chunks didn't grow | Missing Python dep on PYTHONPATH (`pypdf` / `pypdfium2`) | Chart should bake them in (≥ v0.30.5); restart daemon. PLINTH skill `daemon dep verify`. |
| 401 mid-run | Box dev token expired | Switch to JWT auth (this README). Bridge: rotate dev token + restart. |
| Read-only filesystem at materialization | Dagster I/O storage on ConfigMap mount | Chart mounts `dagster-storage` emptyDir (≥ v0.30.5). |
| OCR fallback fails to import | `pypdfium2` / `pytesseract` missing | `pip install` to PVC; verify PYTHONPATH. Chart handles ≥ v0.30.5. |

## Related

- `feedback_rag_dedup_three_tiers` — dedup architecture
- `feedback_plinth_ingest_fix_ingest_loop` — agent that drives the loop
- `feedback_plinth_trace_is_the_primitive` — diagnosis pattern
- `feedback_axi_data_is_misnamed` — noun rename plan
- ADR-049, ADR-052, ADR-057, ADR-062, ADR-061 — architectural decisions
- Tickets: #357, #358, #359, #362, #386, #434, #439, #441, #442

## Status

- v0.30.4: connector hardening + OCR fork + eval harness scaffold
- v0.30.5 (in flight): chart resilience + JWT auth → ends manual token churn
- Roadmap: parallelization (#439), KEEP MCP/A2A, silver tier, `axi data` tap
