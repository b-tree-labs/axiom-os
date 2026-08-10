# Harness-memory landscape survey (2026-07)

**Role:** research reference backing ADR-087 D8 (absorb adapters) and the
cross-mem PRD F3. Snapshot of a fast-moving field — verify before relying on a
specific path/format at build time. Method: vendor docs/changelogs plus
filesystem reconnaissance of default install locations.

## The four memory models (one absorb adapter each)

Twenty products surveyed collapse into four clusters. Most products span two
layers: an **authored instruction-file layer** and an **auto-extracted memory
layer**.

### 1. Markdown-hierarchy instruction files
Claude Code (`CLAUDE.md` hierarchy + auto-memory markdown dirs), Codex
(`AGENTS.md`), Gemini CLI (`GEMINI.md` + `save_memory` append), Cline
(`.clinerules`, memory-bank convention), Roo Code (`.roo/rules`), Continue
(`.continue/rules`), OpenHands (`.openhands/microagents`), Zed (`.rules`),
Amp (`AGENTS.md`), Aider (`CONVENTIONS.md`), OpenCode (`AGENTS.md`), Replit
(`replit.md`).
**Adapter:** hierarchy walk + YAML/MDC frontmatter parse.
**Note:** `AGENTS.md` is the emerging cross-vendor convention — read by most
of the field, making it the best single write-back target.

### 2. Local structured stores
Codex local memories (per-app SQLite: staged memory rows, usage counts,
thread goals), Docker cagent (SQLite `memories(id, created_at, memory,
category)`), Goose (category `.txt` files with tag headers), Hermes Agent
(`~/.hermes/memories/MEMORY.md` + `USER.md`, §-delimited entries, FTS5 session
DB), Letta self-hosted (SQLite/Postgres blocks + passages).
**Adapter:** structured-text/SQLite reader. **Read-only** — these schemas are
app-owned and churn across versions (migration tables observed); parse, never
depend on, never write.

### 3. Vector / passage stores
Letta archival memory (pgvector passages, rich per-passage metadata), Amp
threads (cloud, searchable), Continue→Mem0 (third-party vector memory), Hermes
session search (FTS5).
**Adapter:** passage ingest with provenance mapping.
**Note:** Letta has the richest provenance model and a documented agent-file
export format (blocks + history; archival passages excluded; secrets nulled) —
a useful reference point for cross-mem's own bundle design.

### 4. Cloud account-bound memories
Cursor Memories (server-side, no API), GitHub Copilot Memory (cloud, repo-fact
citations, unused-entry expiry, no API), ChatGPT account Memory (no export),
Devin Knowledge (cloud **with full REST CRUD** — the round-trippable
exception), Amp threads (OpenAPI), Letta Cloud (REST + export), Replit
(workspace-bound).
**Adapter:** per-vendor API client where an API exists; otherwise absorption
is limited to the product's authored-file layer or user-triggered export.

## Portability tiers

| Tier | Products | Absorb path |
|---|---|---|
| File-portable (easy) | Claude Code, Codex (files + local DB), Gemini, Aider, Cline, Roo, Goose, Continue rules, OpenHands, Zed, Hermes, cagent, OpenCode, Letta self-hosted | Read off disk |
| Cloud with real API | Devin Knowledge, Amp threads, Letta Cloud | API/export client |
| Cloud-locked (hard) | Cursor Memories, Copilot Memory, ChatGPT Memory, Replit; Windsurf memories (local but undocumented format) | Authored-file layer only, or user-triggered export |

## Write-back (sync) assessment

The clean bidirectional channel for nearly every product is the **authored
instruction-file layer, not the auto-memory store**. Auto-memory stores are
read-only in practice: cloud without APIs, opaque local formats, or actively
rewritten by the vendor's own extractor (writing there would be fought).

- **Primary write-back target:** `AGENTS.md` (one artifact, widest reach).
- **Fallbacks:** per-product rules files (`.cursor/rules`,
  `.github/copilot-instructions.md`, `.windsurf/rules`, `CLAUDE.md`, …).
- **API write-back:** Devin Knowledge only (genuinely round-trippable).
- **Never write:** app-owned local databases (cluster 2 caveat).

## Disambiguation notes

- **"Hermes"** names both a model family and (since early 2026) a distinct
  agent harness with the local memory store described above; cross-mem targets
  the harness.
- Several products expose *rules* (authored) and *memories* (auto) under one
  marketing name; the adapter clusters treat the layers separately.

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
