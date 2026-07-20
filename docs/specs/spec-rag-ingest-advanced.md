# Axiom RAG — Advanced Ingest UX

**Status:** Implemented (core) — see "Implementation status" below
**Owner:** Ben Booth
**Created:** 2026-05-22
**Last Updated:** 2026-05-27
**Related:** `spec-rag-architecture.md`, `spec-rag-knowledge-maturity.md`, `adr-014-rag-tiered-local-cache.md`, `adr-016-multi-node-federation.md`, `spec-federation.md`

---

## Implementation status (2026-05-27, shipped in v0.22.0)

Built test-first across `axiom.rag.ingest_cli`, `ingest_checkpoint`,
`ingest_preflight`, `ingest_progress`, `ingest_calibration`, and wired into
`rag/cli.py` as the `ingest` verb (replacing the legacy index-alias):

| § | Capability | Status |
|---|---|---|
| §3 | `axi rag ingest` verb + flags + `--dry-run` + CLI subprocess smokes | ✅ shipped |
| §4 | Preflight (scan, reachability, capacity abort-with-numbers, chunk-size advice) | ✅ shipped |
| §5 | Calibration & ETA (rolling throughput, 1.3× multiplier, slow-abort) | ✅ shipped (logic) |
| §6 | Progress event stream (`ProgressState` + JSON sink + TTY text) | ✅ shipped |
| §7 | Checkpoint/resume core (batch-granularity, target/corpus mismatch refusal) | ✅ shipped (logic) |
| §7 | SIGINT double-tap coordinator + per-batch retry/backoff (`run_ingest`) | ✅ shipped (logic) |

**Live path today** runs on the hardened engine (`ingest_path`: file-level
checksum resume + honest drop reporting + embed-failure durability), so a
re-run resumes at file granularity and dropped/controlled files are reported.

**Remaining on this spec's roadmap:**

- **U5b-live** — attach `run_ingest`'s batch-level checkpoint to a real
  per-batch chunk→embed→store executor on the live path (the progress stream,
  calibration, and SIGINT renderers all wire in here). Needs the fixture-Postgres
  integration test of §10.
- **U6** — federated SSH-tunnel `--target <peer>` (§8), gated on the peer
  `rag_ingest` capability descriptor.
- **U7** — proof prompt (§9).
- `--json` heartbeat + `--resume`-from-checkpoint surfaces exist; their
  end-to-end exercise lands with U5b-live's integration test.

**Adjacent (shipped, see `spec-rag-architecture`):** an export-control
**provenance gate** (`ingest_router` — exclude/quarantine/allow-by-source,
`--rules`, safe-by-default shared-tier refusal) runs in front of this engine,
and an **`axi rag audit`** verb audits/purges an existing corpus against the
same rules.

---

## 1. Motivation

`axi rag index` (alias `ingest`) works for small developer-scale corpora: a few markdown files, a quick `pytest`, done. It breaks on the operating profile a consumer layer now needs to support:

- **Tens of thousands of documents** from a single user-supplied corpus (Box folder, archive dump, document mirror).
- **Multi-hour wall-clock ingests** that must survive laptop sleep, VPN drops, and accidental SIGINT.
- **A choice of destination**: the user's local store, or a federation peer's remote store on a private network.
- **A non-expert running the command** who needs honest answers about disk space, time, and whether it worked.

Today's surface answers none of those. It treats every run as "small, synchronous, local, succeeds-or-doesn't." This spec adds the missing UX layer without changing the ingest *engine* (chunker, embeddings, store).

Domain consumers (e.g. an extension layer wrapping `axi rag` for a vertical) get this for free by inheriting the new flags. None of the new behavior is domain-specific.

---

## 2. Goals / Non-Goals

**Goals.**

1. Preflight before any work: input scan, destination capacity, reachability, predicted size.
2. ETA, calibrated from a sample of the actual corpus on the actual destination — not a hardcoded throughput number.
3. Long-running ingest that resumes cleanly after SIGINT, network drop, or laptop sleep, without re-embedding completed chunks.
4. Beautiful, terminal-honest progress: global bar, current file, chunks/sec, embeddings backend, retries.
5. A `--target` selector that points the same command at either a local store or a federation peer's store.
6. A completion announcement that includes a **proof prompt** the user can paste into any agent to confirm the new content is retrievable.

**Non-goals.**

- Replacing or rewriting `axi rag.ingest.ingest_file()`. New UX wraps it.
- New embedding providers or chunkers (those live in their own specs).
- A new HTTP ingest API. Federated target uses tunneled access to the peer's existing pgvector store in v1; an HTTP path is future work and tracked separately.
- Promotion / generation / rollback workflows. Those already have verbs and remain unchanged.

---

## 3. CLI Surface

New verb, alongside `index`. `index` keeps its current contract for scripts that depend on it.

```
axi rag ingest <PATH>...
  --corpus {rag-community|rag-org|rag-internal}   # default: rag-internal
  --target {local|<peer-name>}                    # default: local
  --resume                                        # resume from checkpoint
  --dry-run                                       # preflight only, no writes
  --yes                                           # skip confirmation prompts
  --calibration-sample N                          # default: 50 chunks
  --max-retries N                                 # default: 5 (per chunk batch)
  --checkpoint-dir PATH                           # default: .axi/rag-ingest/
  --json                                          # machine-readable progress + result
```

`PATH` accepts files, directories (walked), or globs. `<peer-name>` resolves against the federation peer registry (`axi federation list`).

---

## 4. Preflight

Runs unconditionally before any embed call. Order matters: cheapest checks first, so we fail fast.

1. **Input scan.** Walk `PATH`, count files by supported extension (`SUPPORTED_EXTENSIONS` from `axiom.rag.extract`), sum raw bytes, count files already present in the destination by `(rel_path, checksum)` (these will be skipped per existing `ingest_file` logic).
2. **Destination reachability.**
   - `local`: confirm `DATABASE_URL` is set, store schema exists, embedding provider responds.
   - `<peer-name>`: resolve peer, open the access channel (see §8), confirm same.
3. **Capacity check.** Estimate new bytes-on-disk as `total_chunks_estimated × avg_chunk_bytes × (1 + embedding_overhead)` where `embedding_overhead ≈ 4× chunk size` for a 768-dim float32 vector with index overhead. Compare to free space reported by the store driver. If estimate exceeds free space, abort with a numeric explanation.
4. **Chunk-size sanity.** If the corpus is dominated by very small (< 200 bytes) or very large (> 50KB) files, surface a one-line note recommending a different chunker tier. Don't block — just inform.

Preflight output is a single Rich panel: file count, total MB, predicted chunks, predicted destination MB, predicted runtime *placeholder* (real ETA comes from §5), space available. Then a confirmation prompt unless `--yes` or `--json`.

---

## 5. Calibration & ETA

Hardcoded throughput numbers age badly across embedding backends, hardware, and network conditions. ETA is computed from a sample of *this run* against *this destination*.

1. After preflight confirmation, run the first `--calibration-sample` chunks (default 50) end-to-end: chunk → embed → write.
2. Record elapsed wall time, mean chunk size, embedding-call latency p50/p95, write latency p50/p95.
3. Project remaining time as `(remaining_chunks / chunks_per_sec_observed)` with a 1.3× safety multiplier for tail latency. Show the multiplier in the UI; users can recognize when it's pessimistic.
4. Re-calibrate continuously: maintain a rolling 200-chunk window for chunks/sec, so a slowdown (network degradation, embedding backend throttling) updates the displayed ETA within seconds.
5. If calibration itself takes more than 60s, abort with diagnostic info — that means the embedding backend or store is unhealthy and the full run will be worse.

---

## 6. Progress UI

Built on `rich.progress`. Two layouts:

**TTY layout** (default when stdout is a terminal):

```
Ingesting <PATH> → <target>/<corpus>
─────────────────────────────────────────────────────────────────────
[████████████░░░░░░░░░░░░░░░░░░░░] 38% · 1,247 / 3,280 files
   current: docs/regulatory/10cfr20-subpart-c.md  (chunk 14/22)
   throughput: 47 chunks/s · embed p95 180ms · write p95 12ms
   ETA: 23m 14s    started: 14:02:11    elapsed: 14m 02s
   embedding: nomic-embed-text (local Ollama)   destination: peer:foo-east

   skipped (already indexed):  412 files
   retried:                     3 chunk batches (last: transient network)
   checkpoint:                 .axi/rag-ingest/run-2026-05-22-1402.checkpoint
─────────────────────────────────────────────────────────────────────
Ctrl-C once to checkpoint and exit; twice to abort without checkpoint.
```

**Headless layout** (`--json`, CI, redirected output):

One JSON object per file completion *plus* a heartbeat object every 5s, suitable for piping into a log collector or a wrapping watcher.

Both layouts use the same event stream internally; the TTY layout is a renderer over it. This is what makes the tests in §10 viable without a terminal.

---

## 7. Checkpoint & Resume

A long ingest that can't survive a SIGINT or a VPN drop is not actually long-running; it's a brittle one. The contract is: at any moment, killing the process and re-running with `--resume` continues from the last completed chunk batch with no double-work.

**Checkpoint file.** JSONL at `${checkpoint-dir}/run-${ISO-timestamp}.checkpoint`. One line per **completed chunk batch** (not per chunk — batch granularity is enough and keeps write amplification down). Schema:

```jsonl
{"ts": "...", "file": "rel/path.md", "checksum": "md5...", "chunk_range": [0, 24], "batch_id": "...", "destination": "peer:foo-east", "corpus": "rag-org"}
{"ts": "...", "file": "rel/path.md", "checksum": "md5...", "chunk_range": [24, 48], "batch_id": "...", ...}
```

Writes use `locked_append_jsonl` (per ADR-011) to survive concurrent processes and crashes mid-write.

**Resume semantics.**

1. `--resume` with no further args picks the most-recent checkpoint in `--checkpoint-dir`. Explicit path supported via `--resume-from PATH`.
2. The walker rebuilds the work set, intersects with completed `(file, checksum, chunk_range)` triples, and emits only the difference. If a file's checksum changed since the checkpoint, the file is fully re-ingested (the chunk ranges no longer apply).
3. Refuse to resume into a different `--target` or `--corpus` than the checkpoint records. Print the mismatch and require an explicit `--force-target-change`.

**SIGINT.**

- First SIGINT: flush in-flight batch, fsync the checkpoint, print the resume command verbatim, exit 0.
- Second SIGINT (within 2s of the first): hard abort without flushing. The checkpoint reflects only batches completed before the first SIGINT, so this loses at most the in-flight batch.

**Network drops.** Per chunk-batch, retry with exponential backoff (`max-retries`, default 5; base 1s, cap 60s). After exhaustion, checkpoint the failure and continue with the next batch. The completion summary lists files that hit max-retry so the user can re-run on them with a narrowed path argument.

---

## 8. Federated Target Adapter (v1: tunneled DB)

**Constraint.** The federation gateway protocol today serves *queries*. It has no upload endpoint and adding one is a separable workstream (auth, quota, audit, RAG-write ACL). To unblock the ingest UX without that work, v1 ingests to a peer via a **tunneled connection to the peer's pgvector store**.

**Mechanism.**

1. `--target <peer-name>` resolves the peer via the federation registry (`axi federation peers`). The peer record must include a `rag_ingest` capability descriptor (new field) with: SSH host/port, SSH user, postgres host/port within the peer's network, and the database role to assume.
2. The adapter opens an SSH tunnel from a local random port to the peer's postgres host:port, using the user's SSH agent. No bare passwords on disk.
3. It rewrites `DATABASE_URL` for the duration of the ingest to point at `127.0.0.1:<local-port>`, with the role from the peer record. Everything downstream of that env var — chunker, embeddings, store — is unchanged.
4. On completion (or abort), the tunnel closes. Nothing on the peer's filesystem changes outside the database write.

**Why SSH and not a new HTTP API.** SSH and a postgres role are existing primitives every peer already operates. We are not designing a federation protocol surface here; we are giving an authorized user the same DB write access they could obtain manually, with safer ergonomics. When the federation gateway grows a real authenticated `/ingest` endpoint, this adapter swaps out behind the same `--target` flag and users see no UX change.

**Refusals.** The adapter refuses if:
- The peer's `rag_ingest` capability is absent or marked `disabled`.
- The local user lacks SSH access to the peer (the `ssh -o BatchMode=yes` preflight fails).
- The peer's reported corpus tier conflicts with `--corpus` (e.g. the user requests `rag-community` on a peer that has not opted in to community publishing).

**Audit.** Every federated ingest writes an audit fragment via CompositionService (`spec-rag-architecture.md` §4) tagged `rag_ingest_federated` with `(peer, corpus, file_count, chunk_count, started_at, ended_at, checkpoint_path)`. The peer can mirror this to its own audit log via the federation event bus.

---

## 9. Proof Prompt

A "the bytes are there" check is necessary but not sufficient. We want to demonstrate that the content is retrievable through the *retrieval path the user's agent will actually use*, not just that it landed in a table.

After successful ingest, sample 3 chunks from the just-indexed set, biased to high-token-overlap distinctness vs the rest of the corpus (so the prompt's answer is unlikely to be a confabulation from a training prior). For each, render a templated question whose correct answer requires content from that chunk. Pick the strongest of the three (most distinctive content, cleanest sentence) and display it as the proof prompt.

Render shape:

```
✓ Ingest complete.
   files indexed:  3,041   chunks created: 28,712
   files skipped:  239 (already up to date)
   destination:    peer:foo-east / rag-org

Proof prompt — paste this into any agent connected to the same corpus:

   "According to <source-doc-title>, what is the exact value of
    <specific-figure-or-claim from sampled chunk>? Cite the source."

A correct, citing answer confirms retrieval is reaching the new content.
```

If the corpus is too small for distinctness (< 20 chunks total), fall back to a generic "ask the agent to summarize <newly-ingested-file-X>" prompt and say so honestly.

---

## 10. Testing Strategy

Tests precede implementation per the project TDD invariant.

**Unit.**

- Preflight cases: empty input, all-already-indexed input, oversize-vs-capacity, unreachable destination, mixed supported/unsupported extensions.
- Calibration: synthetic store with injected latency, assert ETA within ±25% of ground truth at the end of calibration.
- Checkpoint round-trip: write N batches, kill, resume, assert exactly the missing chunks are re-ingested and total chunk count is correct.
- SIGINT handlers: first-SIGINT flushes, second aborts. Use signals in a subprocess.

**Integration.**

- Local target end-to-end against a real (test-fixture) postgres + a fake embedding provider that returns deterministic vectors.
- Federated target end-to-end against a second postgres instance reached via a real SSH tunnel to localhost on a non-standard port. The SSH-tunnel adapter is the new code; this is where it has to be exercised.
- Network-drop simulation: an embedding provider that fails the third batch, recovers on retry 3.

**CLI subprocess smokes** (the project convention).

For each new verb / flag, a test that runs `python -m axiom.cli rag ingest …` as a subprocess and asserts on stdout. Specifically: `--dry-run` shape, `--json` heartbeat shape, `--resume` from a fixture checkpoint, the proof-prompt final block.

---

## 11. Compatibility & Migration

- `axi rag index` keeps its current contract. The new code lives in `axiom.rag.ingest_cli` (or similar) and reuses `ingest_file` and the store API. No schema change.
- The connect-preset shape used by domain consumers (`axiom-extension.toml` `[[connect.preset]]`) is not affected; presets still describe LLM and RAG read endpoints. Ingest is a CLI-driven action, not a preset.
- The `rag_ingest` capability field on peer records (§8) is additive. Peers without it are simply not valid `--target` values; that's the desired failure mode.

---

## 12. Open Questions

1. **Per-file vs per-batch checkpoint granularity.** §7 specifies per-batch (typically ~16 chunks). Per-file would simplify resume logic; per-batch survives mid-file kills. Going with per-batch unless that turns out to cost meaningful throughput.
2. **Proof-prompt distinctness scoring.** Initial heuristic is tf-idf against the rest of the destination corpus. A learned ranker is overkill at this scale; revisit if proof prompts feel weak in practice.
3. **HTTP `/ingest` endpoint as v2.** Tracked separately; the `--target` flag is the stable user-facing surface across both v1 (tunneled DB) and v2 (HTTP). Spec the API once a real auth scheme on the gateway exists.
4. **Cross-platform SSH tunnel.** Per the project's cross-platform support requirement, the tunnel adapter must work on macOS, systemd-Linux, and Windows. On Windows native, `ssh.exe` from OpenSSH-Win32 is the assumed path; document if WSL2 is the only supported Windows config.
