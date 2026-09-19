# ADR-112 — DRY the versioned-artifact substrate: finish the extractions, converge the registries, one conflict surface

**Status:** Accepted — implemented 2026-09-16 (D1–D5 all landed: PRs #870/872/873/874/875)
**Deciders:** Benjamin Booth
**Related:** ADR-110 (connector cohesion — the `ConnectorRegistry` mechanism this finishes applying), ADR-062 (storage connector Protocol — the file-store seam kept separate here), ADR-074 (registry fabric / connection_ref), ADR-087 (memory sync — the other conflict engine), the document-mirror PRD (`publishing/docs/prd-document-mirror.md`).

---

## Context

A survey of the platform's **versioning, conflict/reconciliation, registry, and content-storage** concerns finds no single substrate — there are N, and the split is *mostly* principled but with specific real duplications. Grounding facts:

- **A consumer model catalog ("Model Corral")** stores models as **immutable, version-pinned refs** (`<catalog>:<model>@N`): a new version is a new ref, resolution is an explicit pin plus a `status` promotion pointer. It has **no conflict problem** — immutability sidesteps reconciliation. Its *bytes* already ride the shared `infra.storage` seam; its version-pinned checks ride the data-platform `CheckRegistry`. So it solves a *different* problem from the document mirror (which reconciles a single **mutable** document) — but they share versioning and storage.
- **Content storage is duplicated.** `infra/storage` and `publishing/providers` each define their **own** `StorageProvider` ABC + `UploadResult` + `StorageEntry` of identical shape — and `infra/storage`'s own docstring says it was "extracted from PRESS to be reusable." The extraction happened; PRESS was never repointed at it, so it still runs on a private twin.
- **The registry convergence is unfinished.** ADR-110 §Decision-1 unified six kind→factory families onto `ConnectorRegistry`, and its docstring names **secrets** as one it absorbed — but `secrets/providers/registry.py` still hand-rolls the identical `kind→class` map. The family called "the template" is the one not on the mechanism.
- **Conflict is re-implemented twice, from scratch.** The mirror does *preserve-both-never-guess* (`.mine` conflict copies); memory-sync does *last-writer-wins by event time, loser parked in a shared review queue*. Zero shared code, yet both express the same "never lose data, keep both" instinct — with *correctly different* winner policies.
- **The two stored-bytes version fields** (`UploadResult.version`, `PutReceipt.revision`) are the same concept named twice.

**Forward context (informs the shape, not built here).** The consumer model catalog is intended to generalize to **any ML model** — a Hugging-Face-style hub, and eventually a Hugging Face connector — so that Axiom is a preferred platform for ML model development. A DRY *versioned-artifact + storage + registry* foundation is exactly what a general model hub needs; this ADR builds that foundation.

## Decision

Do the DRY that is genuinely duplicative; keep the separations that earn their keep. Five parts.

### 1. Finish the storage extraction

`infra.storage` is **the** object store. Repoint `publishing/providers` at it and **delete** the twin `StorageProvider` ABC + `UploadResult`/`StorageEntry`. Pure dedup, no behavior change; PRESS keeps working through the shared seam.

### 2. Converge the secrets provider registry

`secrets/providers/registry.py` becomes a thin wrapper over `ConnectorRegistry` (ADR-110 §Decision-1), like directory/channels/editors/storage/data_platform — closing the loop the `ConnectorRegistry` docstring already claims. Behaviour-preserving (its capability/`kind` validation stays).

### 3. One conflict surface

A shared **`ConflictOutcome`** vocabulary — kept-both semantics, a named home for the loser, one resolution UX — used by *both* the mirror and memory-sync. The engines' winner *policies* stay different (a document mirror must not silently LWW; memory propagation should) — only the vocabulary and the resolution UX are shared. The UX, per the "clear, documented, easily understood" requirement:

- Conflict copies are named **`.conflict`** (not the SVN/Perforce-ish `.mine`).
- **Sidecar, not inline markers.** Git writes `<<<<<<<`/`=======`/`>>>>>>>` into the working file; a live byte-mirror must not — the watcher would push the marker-filled text straight back to the remote and corrupt the canonical copy. Instead the canonical remote text stays in the (readable) file and the local edit goes to the `.conflict` sidecar. Same essence — both sides kept, nothing auto-proceeds — made safe for continuous sync.
- A **conflicted state that blocks the next sync until resolved** — mirroring how Git refuses to commit an unmerged file — so an unresolved conflict is never silently pushed.
- A **`resolve` verb** (the "one button" gap): pick yours / theirs / merged, it clears the conflict state and syncs.

### 4. Unify the stored-bytes version field

`UploadResult.version` and `PutReceipt.revision` collapse to one field **name** — `version`, the platform-common term (the mirror's `RemoteDoc.version`, secrets' `?version=`) and the smaller rename — across the two storage families. (The *other* version schemes — the mirror's content-hash, the catalog's `@N`, secrets' `?version=`, memory's `event_time` — ride genuinely different substrates and stay; there is deliberately **no** grand unified `Version` type.)

### 5. Git-repo hygiene (the `.gitignore` question)

When a mirrored document lives in a Git repo, the mirror **auto-maintains a visible, clearly-marked block** in the repo's `.gitignore` covering its own artifacts (`*.conflict`, `*.mirrormeta.json`, the mirror state), idempotently:

```
# >>> axiom mirror (managed) >>>
*.conflict
*.mirrormeta.json
.axi/publisher/mirror-state/
# <<< axiom mirror (managed) <<<
```

Visible-and-marked (not a hidden `.git/info/exclude`) so the user *sees and understands* it; idempotent so it is written once; documented in the mirror docs. And the mirror **composes with Git's own conflict machinery**: a mirror in "conflicted" state is not auto-committed by `git_annotate` (as Git refuses to commit unmerged), and where the doc is genuinely in a Git merge, the mirror defers to Git's markers rather than fighting them.

### Deliberately NOT unified (the DRY floor)

- The mirror's versioned `RemoteEditorEndpoint` vs the file-store `StorageConnectorProvider` — folding them drops the optimistic-concurrency invariant (ADR-110 §Decision-5).
- The catalog's immutable `@N` vs the mirror's mutable-with-conflict — different problems.
- The distinct version *substrates* — no grand `Version` type.

## Consequences

**Positive.** One object store instead of two twins; one registry mechanism across *every* kind→factory family including secrets; one conflict vocabulary + one resolution UX (clear, documented); predictable `.gitignore` behaviour; and a versioned-artifact + storage + registry foundation general enough to grow the model catalog into an any-ML-model / Hugging-Face-style hub.

**Negative / risk.** Touches load-bearing storage + secrets + the mirror. Therefore **phased, TDD-first, every phase ships value, no big-bang**; behaviour-preserving convergences guarded by existing suites; the conflict-UX change is documented and user-facing, so it ships with docs.

## Implementation status

- **D1 (one object store) — landed** (PR #870): `publishing` re-exports
  `StorageProvider`/`UploadResult`/`StorageEntry` from `infra.storage`; twin deleted;
  352 publishing tests unchanged.
- **D2 (secrets registry) — landed** (PR #872): `SecretStoreRegistry` is a thin
  wrapper over `ConnectorRegistry` — the last flat kind→factory family converged;
  401 secrets tests green.
- **D3 (one conflict surface) — landed**: shared `ConflictOutcome` in
  `infra/conflict.py`; conflict copies renamed `.mine` → `.conflict`; the mirror
  now **blocks on conflict** (every pass returns `blocked` until resolved) with a
  `resolve` verb (`--theirs|--ours|--merged`, git-style, re-blocks if the remote
  moved on); wired through the `press.mirror_resolve` skill + `pub mirror resolve`
  CLI; user-facing guide at `publishing/docs/mirror-conflicts.md`. Memory-sync
  adopting the same vocabulary is a documented follow-on (its LWW engine is a
  mature ADR-087 subsystem; only the words are shared, per the DRY floor). The
  chaos storm's convergence invariant was reworked to the block-then-resolve
  doctrine; 373 publishing tests green.

- **D4 (one stored-bytes version field) — landed**: `PutReceipt.revision` renamed
  to `PutReceipt.version` (protocol + local impl), matching
  `UploadResult.version`; the two storage *families* stay separate (DRY floor),
  only the field name is shared; a cross-family guard test pins it against drift.
- **D5 (git-repo hygiene) — landed**: `infra.git.ensure_managed_gitignore` writes a
  visible, marked, idempotent block (`*.conflict`, `*.conflict.*`,
  `*.mirrormeta.json`, `.axi/publisher/mirror-state/`) into the repo's top-level
  `.gitignore`; the mirror calls it on `add` and before a conflict sidecar is
  created; `git_annotate` now defers (commits nothing) while a conflict is
  unresolved. Documented in `publishing/docs/mirror-conflicts.md`. Also hardened a
  flaky wave-two two-engine chaos test that could end blocked under the D3
  doctrine.

**The DRY-substrate program (ADR-112) is complete: D1–D5 all landed.**

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
