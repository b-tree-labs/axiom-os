# System Limits & Known Constraints

**Status:** Active — updated with each chaos round
**Owner:** Ben Booth
**Created:** 2026-04-08
**Last Updated:** 2026-04-08
**Related:** `spec-rag-architecture.md`, `spec-federation.md`, `spec-knowledge-graph.md`

---

## Purpose

This document catalogs the known limits, constraints, and failure modes of the Axiom platform. It is updated after each chaos test round, pentest, or production incident. The goal is honest visibility — knowing what the system can't do is as important as knowing what it can.

---

## 1. RAG Corpus Limits

| Limit | Current Value | Notes |
|---|---|---|
| Max corpus size tested | 256k chunks / ~9k docs | Single PG instance, no performance issues observed |
| Max single chunk size | 800 chars (configurable) | Larger chunks waste context window; smaller chunks lose coherence. Semantic chunking (§12a of spec-rag-architecture.md) replaces fixed-size in Phase 0.1+ |
| Embedding dimension | 1536 or 768 | Depends on provider; mixing dimensions in same corpus not supported |
| ivfflat index | Must be created post-ingest | Requires rows to exist before `CREATE INDEX ... WITH (lists = 100)` |
| Scanned PDF quality | Variable | OCR (ocrmypdf + Tesseract) quality depends on scan resolution |
| Supported ingest formats | .pdf, .docx, .pptx, .odt, .txt, .md, .xlsx, .doc | .doc requires `antiword` CLI; .xlsx extracts minimal text; images/CAD/binary not supported |
| NUL bytes in content | Must be stripped | PDF and text files can contain \x00; causes PostgreSQL `ValueError` if not cleaned |

## 2. Federation Limits

| Limit | Current Value | Notes |
|---|---|---|
| Max peers tested | 3 (mock) | Real-world multi-node testing pending; ThreadPoolExecutor max_workers=8 |
| Peer timeout | 1.5s default, adaptive to 3s | Peers > 3s RTT contribute nothing to interactive chat |
| Max concurrent peer queries | 8 (ThreadPoolExecutor) | Could bottleneck with > 8 federated peers |
| Circuit breaker threshold | 3 consecutive failures | Peer auto-disabled after 3 fails; half-open probe after 5min |
| Signature algorithm | Ed25519 only | No algorithm negotiation; no fallback to weaker algorithms (by design) |
| Replay window | 5 minutes | Requests with timestamps > 5min old are rejected; requires reasonable clock sync between nodes |
| Max request body | 64KB | Limits query + embedding payload; 768-float embedding ≈ 6KB |
| Max response payload | 256KB | 50 chunks × ~5KB each ≈ 250KB |
| Embedding in request | Optional but recommended | Saves 100-500ms per peer by avoiding re-embedding; adds ~6KB to request |

### 2.1 Federation Security Findings (Chaos Round 2026-04-08)

| Finding | Severity | Status | Details |
|---|---|---|---|
| **`verify_content()` argument order bug** | HIGH | FIXED | `security.py` called `verify_signature(content, sig, key)` but correct order is `(key, content, sig)`. Was hidden by SHA-256 hash fallback. Fixed by correcting arg order and removing insecure fallback. |
| **SHA-256 hash fallback removed** | MEDIUM | FIXED | `SecurityService.verify_content()` fell back to SHA-256 hash comparison when Ed25519 failed. This is insecure — an attacker could forge a "signature" by just hashing the content. Fallback now rejects instead. |
| **No rate limiting on API endpoint** | LOW | DEFERRED | Auth + circuit breaker provide basic protection; formal rate limiting (token bucket per node) planned for Phase 0.2 |

## 3. Agent Limits

| Limit | Current Value | Notes |
|---|---|---|
| RAG context injection | Max 4 chunks × 600 chars | Configurable via `_rag_context(limit=4)`; more chunks = more context = higher cost |
| Federated context annotation | Text-only provenance | `[via node:abc123]` — LLM sees provenance but cannot verify it |
| Trust-aware reasoning | Not yet in system prompt | Agents don't yet receive instructions on how to weight low-trust sources; needs prompt engineering |
| Cross-agent communication | Not implemented | Agents operate independently; no inter-agent message bus for federation events yet |
| Agent process supervision | Watchdog added (30s startup timeout) | CLI self-terminates if startup exceeds 30s; long-running agent watchdog still needed |

### 3.1 Agent Skills Shipped

| Skill | Class | What It Does | Limit |
|---|---|---|---|
| CircuitBreaker | `federation.CircuitBreaker` | Auto-disables failing peers | Fixed threshold (3); no adaptive/ML-based detection |
| PeerHealthTracker | `federation.PeerHealthTracker` | RTT + error rate tracking | In-memory only; lost on restart |
| ContentValidator | `federation.validate_peer_results()` | Rejects oversized/poisoned chunks | Static rules only; no learned patterns |
| FederationStatus | `federation.FederationStatus` | Degradation mode awareness | Reports only; doesn't trigger agent behavior changes yet |

## 4. Infrastructure Limits

| Limit | Current Value | Notes |
|---|---|---|
| PostgreSQL | Single instance, no HA | PG crash = full outage; no replication |
| TLS | Self-signed on first deployment | Real certificate needed before public endpoint |
| LLM hosting | Single GPU, single model | No multi-model or multi-GPU scheduling |
| Storage | Local disk only | No distributed storage; SeaweedFS planned for future |

## 5. CLI / Process Limits

### 5.1 Findings (Chaos Round 2026-04-08)

| Finding | Severity | Status | Details |
|---|---|---|---|
| **CLI commands can hang indefinitely** | HIGH | FIXED | CLI startup imports all extension modules for tab completion. Any extension that blocks (DB connect, network call) hangs the entire CLI. Fixed with 30s watchdog thread that calls `os._exit(1)`. |
| **CLI with invalid subcommand spins at 100% CPU** | HIGH | FIXED | Pre-dispatch hooks (`_check_and_prompt_update`, `_show_pending_changelog`) could block. Fixed with `AXIOM_NO_UPDATE_CHECK` env var + watchdog. |
| **Test processes can outlive pytest** | MEDIUM | FIXED | Mock HTTP servers and subprocess-spawned CLIs survived test runner exit. Fixed with: `pytest-timeout=60`, `daemon=True` threads, autouse conftest fixture that kills orphaned child processes. |
| **No long-running agent watchdog** | MEDIUM | OPEN | CLI startup watchdog covers short commands. Long-running agents (chat, serve) need a separate heartbeat/watchdog mechanism. |

## 6. Data Limits

| Limit | Current Value | Notes |
|---|---|---|
| Content-hash dedup | SHA-256 of extracted text | Catches identical content under different filenames; does NOT catch near-duplicates (minor edits) |
| Near-duplicate detection | Cosine > 0.97 at query time | Runtime dedup only; duplicates still stored and embedded |
| OCR quality | Tesseract via ocrmypdf | Good for clean scans; poor for faded/skewed documents; no confidence scoring per page |
| Time-series data | **Not supported** | Structured/time-series data needs a dedicated pipeline (DuckDB or PG tables), not RAG chunking |
| Export-controlled data | Must remain on authorized infrastructure | EC code never on general-purpose servers; I/O can feed restricted-tier RAG |

## 7. Testing Limits

| Metric | Current Value |
|---|---|
| Total federation tests | 73 (Steps 1-5) + 17 (chaos) + 30 (security) = 120 |
| Total pre-existing tests | ~254 (federation infra) + agent lifecycle + tools |
| Integration tests | Mock peers only; no real multi-node test yet |
| Penetration testing | Not yet performed |
| Load testing | Not yet performed; unknown behavior at > 100 concurrent queries |
| Soak testing | Not yet performed; unknown memory/connection leak behavior over hours |

## 8. Architecture Decisions Made This Round

| Decision | What Changed | Impact |
|---|---|---|
| **Hybrid graph extraction** | Graph entities extracted from source docs, NOT RAG chunks. Two parallel pipelines from source. | Better entity quality; chunks informed by graph structure; `spec-knowledge-graph.md` §3 updated |
| **Semantic chunking** | Graph extraction identifies section/table/procedure boundaries → chunker aligns to semantic units | Breaks are meaningful, not arbitrary; tables stay intact; cross-refs preserved; `spec-rag-architecture.md` §12a added |
| **document_id anchor** | SHA-256 content-hash as universal stable ID across RAG, graph, facts, and retrieval log | Re-ingest preserves all learned knowledge above Layer 0; `spec-rag-architecture.md` §12 added |
| **Truth provenance chain** | Every fact traces back to citable source documents with full citation metadata | Researchers can cite sources for generated papers; `spec-rag-knowledge-maturity.md` §12 added |
| **pipeline_version tracking** | Documents table tracks which pipeline version produced chunks | Targeted re-ingest: only re-process docs from older pipeline versions |
| **SHA-256 fallback removed** | `SecurityService.verify_content()` no longer falls back to hash comparison | Prevents attackers from forging "signatures" by hashing content; `security.py` updated |

---

## 9. What We Don't Know Yet

These are areas where we lack data or testing:

1. **LLM inference quality on large corpus** — unknown how well grounding works with 256k+ chunks
2. **Real-world federation latency** — only tested with localhost mock peers
3. **PG performance at 1M+ chunks** — current 256k is fine; growth trajectory unclear
4. **Embedding model quality** — text-only search operational; vector search quality unknown until embeddings are wired
5. **Agent prompt engineering for federated context** — agents don't yet know how to reason about provenance, trust gradients, or degraded mode
6. **Multi-node consensus** — fact promotion with multi-site validation is designed but not implemented
7. **Graph layer (AGE) stability** — Apache AGE not yet installed; PG 16 compatibility unverified

---

## Update Log

| Date | Round | Key Findings |
|---|---|---|
| 2026-04-08 | Chaos Round 1 | `verify_content()` arg order bug (FIXED), SHA-256 fallback removed (FIXED), CLI hang bugs (FIXED with watchdog + orphan cleanup), pytest-timeout added. Architecture: hybrid graph extraction, semantic chunking, document_id anchor, truth provenance chain. **120 new tests, all green.** |

### Chaos Round 1 — Full Findings

**Bugs Found & Fixed:**
1. `SecurityService.verify_content()` — argument order wrong (`content, sig, key` instead of `key, content, sig`). Hidden by insecure SHA-256 fallback. **Severity: HIGH.** Fixed in `security.py`.
2. SHA-256 hash accepted as valid "signature" — attacker could forge by hashing content. **Severity: HIGH.** Fallback removed.
3. CLI hangs on startup due to extension import blocking. **Severity: HIGH.** Fixed with 30s watchdog thread.
4. CLI `_check_and_prompt_update()` calls `input()` in non-interactive contexts. **Severity: HIGH.** Fixed with env var guard.
5. pytest mock HTTP servers using class variables bleed state between tests. **Severity: MEDIUM.** Fixed with closure-based handler factories.
6. NUL bytes in PDF/text files cause PostgreSQL `ValueError`. **Severity: MEDIUM.** Fixed with `\x00` stripping in ingest.
7. Test processes outlive pytest runner. **Severity: MEDIUM.** Fixed with orphan process cleanup fixture.

**Agent Skills Shipped:**
- CircuitBreaker (auto-disable failing peers)
- PeerHealthTracker (RTT + error rate tracking)
- ContentValidator (reject oversized/poisoned chunks)
- FederationStatus (degradation mode awareness)

**Architecture Decisions:**
- Graph extraction from source docs (not lossy RAG chunks)
- Semantic chunking informed by graph structure
- document_id as universal stable anchor
- Truth provenance chain for citations
- pipeline_version for targeted re-ingest
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
