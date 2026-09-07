# Axiom — Project Memory & AI Assistant Context

This file is the canonical onboarding doc for both human contributors and AI
coding assistants (Claude Code, Cursor, Copilot, Codex, Aider). `CLAUDE.md`
is a symlink to this file so Claude Code picks it up automatically.

---

## What is Axiom?

Axiom is a **domain-agnostic agentic platform** — the substrate domain
consumers (and other consumer layers) build on top of. Core responsibilities:

- **Unified composition memory** — MemoryFragment with immutable `(T, U, A, R)`
  provenance; MIRIX 6-type cognitive taxonomy (core/episodic/semantic/procedural/
  resource/vault); CompositionService as the single entry point for all memory
  ops.
- **Federation** — `axiom://` URI scheme, cohort registry, A2A protocol,
  multi-authority signatures, trust graph. (Pre-extraction of Vega.)
- **Extensions** — classroom, agents, RAG, research loops. Everything
  non-core lives in `src/axiom/extensions/`.

Axiom docs **never name domain consumers** — no references to a specific
domain (e.g. nuclear, reactors, facilities). Consumer-specific material lives
in the consumer's own repo.

## Portfolio Context

Axiom is one of six portfolio products per `docs/working/brand-product-strategy.md`:

| Product | Owner | Role |
|---|---|---|
| **Axiom** | B-Tree Labs (B-Tree Ventures, LLC) | Agent platform / harness |
| **Vega** | The institution | Federation + governance layer (currently in `src/axiom/federation/`, `security/`, `identity/`; consolidates to `src/axiom/vega/` per ADR-031 pre-extraction) |
| **Keplo** | The institution | Classroom + learning analytics (currently `src/axiom/extensions/builtins/classroom/`; extracts to its own repo) |
| **Vyzier** | B-Tree Labs | Polyglot extension marketplace + registry |
| **Domain consumer** | The institution | Domain consumer application (example — lives in its own repo) |

## Naming Conventions

- **Products** use normal case: Axiom, Vega, Keplo, Vyzier
- **Agents** use ALL-CAPS short names: AXI, SCAN, TIDY, PRESS, TRIAGE, CURIO, CHALKE, WARDEN
- **WARDEN** = Verifier, Enforcer, Gatekeeper, Arbiter (Vega's federation agent)
- **CHALKE** = Keplo's classroom agent (owns workflow orchestration + instructor brief)
- Products and agents coexist in prose. The casing unambiguously signals which is which.
- **Extensions** are named by purpose (no type suffix): `classroom/`, `connect/`, `memory/`. Type information lives in the AEOS manifest.

## AEOS — Agent Extension Open Standard

Every extension in the Axiom portfolio conforms to AEOS per [spec-aeos-0.1.md](docs/specs/spec-aeos-0.1.md).

- **AEOS conformance is required** for all Axiom, Keplo, Vyzier, and consumer extensions
- **Standards positioning** is dual-track per ADR-032: publicly contribute to AAIF (MCP, OASF, MCPB, SKILL.md, AGENTS.md); privately maintain AEOS as the internal delta capturing federation-native leap-ahead features
- **Do NOT promote AEOS externally** without strategic decision. Internal documents are in the repo; external advertising is off until triggers in ADR-032 are met.
- Extensions follow the layout in spec-aeos-0.1.md §5: purpose-named directory, compound layout by default, seven capability kinds (agent, tool, cmd, service, adapter, skill, hook), `axiom-extension.toml` manifest, `__all__` public API, Sigstore-signed releases.
- Tooling: `axi ext <verb>` for all lifecycle operations (see spec §10). Use `axi ext init <name>` to scaffold; `axi ext lint` to verify conformance.

---

## Repository Structure

```
axiom/
  src/axiom/
    memory/                # MemoryFragment, CompositionService, ownership, policy
    identity/              # keypair, signatures, principals
    federation/            # cohort registry, A2A, trust graph, classification
    extensions/builtins/
      classroom/           # classroom v1 (course prep, quiz, harvest, promotion, ...)
      chat/          # assistant agent (chat loop, RAG context)
      ...
    infra/                 # gateway, orchestrator, prompt registry, artifact registry
    rag/                   # retrieval store, hybrid search
    policy/                # 4-scope policy engine
  docs/
    adrs/                  # Architecture Decision Records
    prds/                  # Product Requirement Documents
    specs/                 # Technical specifications
    papers/                # Research papers (axiom-composition-emergence, ...)
    working/               # Session checkpoints, in-flight design docs
    reference/             # External citations and reading notes
  tests/                   # Cross-cutting tests (extension tests live alongside code)
  runtime/                 # Instance-specific data (gitignored)
  scripts/                 # Bootstrap and maintenance
```

### Where Does New Code Go?

| I want to... | Location |
|---|---|
| Add a new extension | `src/axiom/extensions/builtins/{purpose-name}/` — use `axi ext init` to scaffold per AEOS |
| Add platform primitives (identity, federation, policy) | `src/axiom/{module}/` |
| **Add persistence to an extension** | **`from axiom.infra.db import session_for` — schema-per-extension; never write to `public`; see ADR-052** |
| Write an ADR | `docs/adrs/adr-NNN-{title}.md` — pick NNN with `python scripts/lint_adr_numbers.py --next`; **don't** hand-pick (collision-prone) |
| Write an extension-level ADR | `src/axiom/extensions/builtins/{ext}/docs/decisions/adr-NNN-{title}.md` per ADR-031 |
| Write a session checkpoint | `docs/working/` |
| Write cross-cutting tests | `tests/` |
| Write extension tests | `src/axiom/extensions/builtins/{ext}/tests/` |
| Write extension docs (PRD, spec) | `src/axiom/extensions/builtins/{ext}/docs/` per ADR-031 |

---

## Load-Bearing Architectural Docs

Read these before making structural changes:

- **ADR-026** — Ownership model (single master + peer delegations; 4 rights)
- **ADR-027** — Federated memory (`axiom://` URI, cohort registry, multi-sig)
- **ADR-028** — Trust graph (EigenTrust-inspired, optimistic defaults)
- **ADR-029** — Federation composition (the four-primitives rule; meta-ADR)
- **ADR-031** — Extension self-containment (docs + tests co-located with extension code)
- **ADR-032** — Standards positioning (dual-track: public AAIF contributions + private AEOS delta)
- **ADR-050** — Tenant/site vocabulary (no "facility" in platform code)
- **ADR-052** — Database tenancy: one Postgres per install, schema-per-extension via `axiom.infra.db.session_for`
- **spec-aeos-0.1.md** — Agent Extension Open Standard (governs every extension)
- **RPE spec** — Retrieval Policy Engine, 8 intents
- **docs/papers/axiom-composition-emergence.md** — Claims CL-1..CL-6 proving
  whole > sum of parts
- **docs/working/brand-product-strategy.md** — Six-product portfolio context
- **docs/working/aeos-playbook.md** — Day-to-day operational guide for extension work

---

## Core Invariants

- **Every memory write goes through `CompositionService`.** Don't bypass it
  for direct fragment construction.
- **Provenance is immutable** — `(T, U, A, R)` tuple fixed at write time.
- **Ownership uses `dataclasses.replace`**, never field-by-field reconstruction
  (silently drops fields).
- **IDs are auto-generated** — callers never invent identifiers on create.
- **Principal naming** — `@name:context` Matrix-style, single `@`.
- **TDD** — tests before implementation, always.
- **Database access** — extensions go through `axiom.infra.db.session_for("<ext>")`. Never construct your own engine, never write to `public`, never hardcode `schema=...` on tables (the provider sets `search_path` per-connection). Cross-extension reads ride the data platform (ADR-049), not OLTP joins. See ADR-052.
- **CLI verbs are thin wrappers over skill functions** — per ADR-056, every CLI verb maps 1:1 to a function registered through `axiom.infra.skills.SkillRegistry`. CLI handler logic NEVER lives inside argparse handlers; it lives in `<ext>/skills/<verb>.py` with shape `(params, ctx) -> SkillResult`. Reference: `data_platform/cli.py` + `data_platform/skills/`. When migrating a verb (rename, grammar fix, anything) you MUST extract its logic into a skill function in the **same PR** — "rename now, skill-fn later" is the wrong PR. See `docs/working/cli-verb-grammar-audit-2026-05-30.md` § Per-migration checklist.
- **CLI nouns are purpose-named, not agent-named** — agent personas (TIDY, PLINTH, RIVET, …) are LLM characters used in reasoning. CLI nouns are the deterministic platform 'arms and legs'. `axi hygiene`, not `axi tidy`. `axi data`, not `axi plinth`. ADR-056 § Layering.

---

## Development Setup

### Environment

- **Venv**: `.venv` at the workspace root (alongside the consumer repo), Python 3.14
- **direnv**: `.envrc` in the consumer repo activates the parent `.venv`; use
  the same venv when working in axiom
- **VS Code**: `python.terminal.activateEnvironment: false` (direnv handles it)

### Testing

```bash
# All tests
pytest tests/ src/axiom/extensions/ -v --tb=short

# Single extension
pytest src/axiom/extensions/builtins/classroom/tests/ -v

# Emergence suite (whole > sum of parts)
pytest tests/emergence/ -v
```

### Prompt Evals (promptfoo)

```bash
cd tests/promptfoo
npx promptfoo eval                    # chat quality
npx promptfoo eval -c rag-evals.yaml  # RAG grounding (requires indexed corpus)
```

---

## Documentation Conventions

- **Mermaid only** (never ASCII art). Vertical TD/TB flow for 8.5×11 portrait.
  Every node and subgraph styled with `fill:` and `color:` for contrast.
- **Axiom docs never name domain consumers.** Use placeholder terms
  ("domain extension", "consumer layer"), not a specific domain's terms
  ("nuclear" / "reactor" / "facility").
- **ADRs** follow MADR-lite: Context → Decision → Consequences, with a
  Status line at the top.

---

## Issue Tracking & Commits

- Issues: GitHub issues on the axiom repo (not Linear — Linear is for
  unrelated projects).
- Every commit includes a `Co-Authored-By:` trailer per the user's memory.
- Don't push, tag, or release during interim build-out unless explicitly
  asked — commit locally.

### Pushing onto red main: override-reason required

Install the repo hooks once: `cp scripts/hooks/pre-push scripts/hooks/commit-msg .git/hooks/ && chmod +x .git/hooks/pre-push .git/hooks/commit-msg`.

When `origin/main` is red, the pre-push hook refuses to push any commit that
lacks a `Bypass-Reason:` trailer. The trailer is not boilerplate — it is the
receipt that says "I know main is red and here is why this push is
necessary anyway." Two ways to get one in:

- Commit with `AXI_OVERRIDE_REASON="why" git commit ...` — the commit-msg hook
  stamps the trailer.
- Push with `AXI_OVERRIDE_REASON="why" git push ...` — if HEAD is the only
  commit missing the trailer, the pre-push hook amends it in for you (HEAD
  sha changes; force-push if you'd already pushed).

`git push --no-verify` still bypasses the hook (Git cannot prevent that).
Every red-main push attempt is appended to `~/.axi/pre-push-bypass.log` —
that log is the audit trail when something compounds on main.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
