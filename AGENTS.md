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

## Which Layer Does This Belong To?

The rule above has been in this file since the beginning, and one test enforced
it against one module. The rule held there and nowhere else, so on 2026-09-25 a
sweep found **68 substrate modules naming a domain** and nobody knew the number.
Ten named a customer outright, and two of those defaulted a CLI argument to one
customer's site id, so every other install was told in writing to map its
connector to somebody else's site.

Almost none of that was an architectural mistake. It is a *good* habit landing
in the wrong layer: anchoring a comment to the real defect it came from, because
a comment tied to an actual failure is worth ten that describe a category. In a
substrate that is still true and the example still does not belong, because the
next consumer reads it and learns a vocabulary that is not theirs.

**Three questions decide the layer.** Ask them before writing the module, not
after.

1. **Strip the domain noun. Is there still a capability?** "Does a declaration
   cover the data it claims to cover?" is a substrate question, and it survives
   losing the word *channel*. "Which reactor console emitted this?" does not
   survive losing the word *reactor*, so it is consumer logic.
2. **Who notices when it is wrong?** If only a consumer's operator can tell that
   the answer is wrong, the knowledge that makes it wrong lives in their repo.
3. **Does it read a consumer's artifact?** A substrate module may not parse a
   consumer's file format, registry or vocabulary. It may define the *shape* a
   consumer fills in. Reading `site_channels.toml` is consumer work even when
   the comparison logic underneath is perfectly generic — in that case split it,
   and keep the generic half here.

Worked example from the same sweep, including one the questions reversed.
Tier classification, duplicate-key collision and gold query shaping are
substrate and stayed. Coverage checking *looked* like consumer work, because
the thing it checks is a map a consumer authors, and the first call was to move
it out. Question 3 said otherwise: the module imports `re` and `dataclasses`,
takes two sets of names as arguments, and never opens a file. The consumer's
own repo already holds the reader that knows the file's format and hands those
sets over. **The instinct was reading the caller's domain off the callee**, and
the split was already right. Where the answer is genuinely mixed, split it and
keep the half that never names an artifact here.

### The guard, and its debt list

`tests/test_axiom_names_no_domain.py` enforces both halves at repo scope.

- **A customer or deployment id has no debt list.** Not one, anywhere, tests
  included. A domain noun teaches the wrong vocabulary; a partner id names a
  customer inside the substrate they are a customer of.
- **A domain noun has a named-debt list, and it only shrinks.** The list names
  every file, rather than bounding a total, because a ceiling lets one file get
  worse while another improves and never tells anybody which files. A new module
  is clean by default. Cleaning a file means deleting its line, and the guard
  fails if a listed file is already clean, so the list stays a to-do rather than
  a permanent exemption. It stood at 68 files and was down to 55 the same
  afternoon, with `data_platform` taken to zero.
- **The noun scan is shipped code; the identity scan is everything.** A fixture
  reads as an example rather than as the platform's own vocabulary, so a domain
  noun in a test is out of scope on purpose. A customer's name in a test is not,
  because the next fixture gets copied from it.

If a guard failure looks like a false positive, say the thing in the substrate's
own terms instead of widening `DOMAIN_NOUNS`. The pattern is deliberately narrow:
*site*, *channel*, *signal* and *unit* are vocabulary any data platform needs,
and flagging those would make the guard unpassable and therefore ignored.

**When a layering call is hard, write the call down here.** The three questions
above are each the residue of getting it wrong once, and the next one will be too.

## Portfolio Context

Axiom is one of the institution's portfolio products. **Shipping today** (code in-tree or in their own repos):

| Product | Owner | Role |
|---|---|---|
| **Axiom** | B-Tree Labs (B-Tree Ventures, LLC) | Agent platform / harness |
| **Vega** | The institution | Federation + governance layer (in `src/axiom/vega/` — `federation`, `identity`; pre-extraction staging per ADR-031 Phase 3) |
| **Keplo** | The institution | Classroom + learning analytics (currently `src/axiom/extensions/builtins/classroom/`; extracts to its own repo) |
| **Domain consumer** | The institution | The domain application — the consumer layer, in its own repo (named in the consumer repo, not here) |

**Not yet built** (idea/concept, not shipped): **Vyzier** (polyglot extension marketplace + registry). Portfolio-adjacent: **SoilMetrix** (separate entity) is a future consumer that will build on Axiom + Vega.

## Naming Conventions

- **Products** use normal case: Axiom, Vega, Keplo (planned: Vyzier). The domain consumer is named only in its own repo.
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
- Extensions follow the layout in spec-aeos-0.1.md §5: purpose-named directory, compound layout by default, eight capability kinds (agent, tool, cmd, service, adapter, skill, hook, prompt), `axiom-extension.toml` manifest, `__all__` public API, Sigstore-signed releases.
- Tooling: `axi ext <verb>` for all lifecycle operations (see spec §10). Use `axi ext init <name>` to scaffold; `axi ext lint` to verify conformance.

---

## Repository Structure

```
axiom/
  src/axiom/
    memory/                # MemoryFragment, CompositionService, ownership, policy
    vega/                  # Vega (pre-extraction staging, ADR-031 Phase 3):
      federation/          # cohort registry, A2A, trust graph, classification
      identity/            # keypair, signatures, principals
    extensions/builtins/
      classroom/           # classroom v1 (course prep, quiz, harvest, promotion, ...)
      chat/          # assistant agent (chat loop, RAG context)
      ...
    infra/                 # gateway, orchestrator, prompt registry, artifact registry
    rag/                   # retrieval store, hybrid search
    policy/                # 4-scope policy engine
  docs/                    # See docs/README.md; standard: docs/conventions/doc-standards.md
    adrs/                  # Architecture Decision Records
    prds/                  # Product Requirement Documents
    specs/                 # Technical specifications
    conventions/           # Portfolio standards (doc-standards, the-axiomatic-way)
    templates/             # Skeletons for new PRDs / specs / ADRs
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
| Add platform primitives (policy, infra) | `src/axiom/{module}/` — identity + federation live under `src/axiom/vega/{module}/` |
| **Add persistence to an extension** | **`from axiom.infra.db import session_for` — schema-per-extension; never write to `public`; see ADR-052** |
| **Report a measured value** | **`from axiom.uncertainty import Quantity` — carry sources, not a scalar; declare a `Budget` per symbol; see ADR-136 + `docs/specs/spec-uncertainty.md` §2** |
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
- **tests/emergence/** — the proof suite for whole > sum of parts (claims CL-1..CL-6)
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
- **Secrets live in the Axiom vault — never in a plaintext env file, a config
  file, a script, or argv.** Store with `axi secrets set` (the value comes from
  stdin or an interactive prompt, never from the command line, so it does not
  land in shell history or a process listing). A service reads its credential
  out of the vault at start-up; it does not get a copy written beside it.

  This is not a preference about tidiness. A key in `foo.env` is readable by
  anything that can read the file, survives in backups and in every copy of the
  host, cannot be rotated without editing files on each machine, and leaves no
  record that it was read. The vault gives rotation (`axi secrets rotate`), an
  audit trail (`axi secrets audit`), and a leak closer (`axi secrets exposed`,
  which records the exposure and force-rotates).

  Corollary for agents: **never print a secret value into a transcript, a log
  or a commit message.** `axi secrets get --reveal` exists for a program to
  consume, not for a human-readable surface — printing one is itself the
  exposure, and the correct response is `axi secrets exposed`, not deletion of
  the message.

- **A measured value carries its uncertainty, and an uncertainty carries its
  sources.** Anywhere an extension produces, conforms, aggregates or serves a
  number that came from measurement or from a model, it goes through
  `axiom.uncertainty` — never a bare float, and never a scalar error bar where
  the sources are known.

  The rule is one step deeper than the two it extends. *A value without its
  unit is not a fact; a ratio without its reference is not a fact;* **an
  uncertainty without its correlation structure is not composable.** A scalar
  cannot be combined: two readings from one instrument share its calibration,
  and averaging them as independent claims a precision the instrument cannot
  deliver — always in the overconfident direction, always invisibly.

  What that means in practice, per `docs/specs/spec-uncertainty.md` §2:

  - Mint a symbol per **independent physical source**, namespaced
    `<extension>:<resource>:<aspect>`. One physical source is ONE symbol
    however many values it touches — a calibration offset shared across a
    thousand readings does not average away, and sharing a symbol is what
    says so.
  - Declare a `Budget` per symbol: the **measurand** (without it an
    uncertainty is undefined, per GUM), how it was evaluated, what it traces
    to, over what range it is valid, and its degrees of freedom.
  - Report absence in one of **three kinds** — sources known, magnitude only,
    or nothing reported. Never zero for unknown: zero is a claim of perfect
    precision.
  - Crossing a process, database or node boundary? `axiom.uncertainty.wire`.
    Two nodes both mint `signals:tc-14:repeatability` for different sensors,
    and unqualified symbols make them look like one source — which *narrows*
    the answer, the direction that is wrong rather than merely unhelpful.
  - Anyone may **widen**; nobody may **narrow** by assertion. Correlation is
    computed from shared symbols, never declared, so there is no field an
    agent can set to claim independence.

  Ask `data.uncertainty_coverage` before quoting a served figure. The
  apparatus can be complete and correct while nothing upstream has declared
  anything, and in that state every aggregate returns `claimable: false` and
  no check anywhere fails.

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

#### Worktrees share one venv, so point your tools at your own source

One virtualenv serves every worktree, and `pip install -e` anchors `axiom` to
whichever checkout ran it. Every other worktree then imports **that** one. The
symptom is a verb that "does not exist" or a fix that "did not work", and the
cause is that you were running somebody else's branch.

- **pytest is already handled.** The root `conftest.py` exports the same roots
  `pyproject.toml` declares in `pythonpath`, so a subprocess test exercises the
  checkout it lives in. Before that, all 100-plus subprocess test files ran the
  anchor worktree's code — silently passing or failing on it, and letting any
  session editing the anchor red-light every other worktree's pre-push gate.
  `tests/test_worktree_isolation.py` is what keeps it true, and it includes a
  negative control that proves the guard can fail.
- **For a shell**, one command fixes every worktree of a repo:

  ```bash
  python scripts/setup_worktree_envs.py                       # this repo
  python scripts/setup_worktree_envs.py --repo ../nos --repo ../CoreForge
  ```

  It writes a self-contained block into each `.envrc` (gitignored, so it cannot
  ship as a file) and runs `direnv allow`. Layouts are detected, so it handles
  `src/`, `packages/*/src`, and a package at the repo root. It never touches a
  repo you did not name — `--all` sweeps the workspace and is opt-in, because
  most repos here are other people's clones.

  Without it, `axi` in your branch runs the anchor's code. `axi` also says so
  when it notices; `AXI_NO_SOURCE_WARNING=1` silences that.

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

The portfolio doc standard is `docs/conventions/doc-standards.md` — one
folder per document kind, the three-document rule, naming, enforcement.
The short version:

- **PRD** (`docs/prds/prd-<noun>.md`) — what a surface must do and why. Living.
- **Tech Spec** (`docs/specs/spec-<noun>.md`) — how a subsystem is built.
  Living — **a PR that changes a subsystem's behavior updates its spec in
  the same PR.**
- **ADR** (`docs/adrs/adr-NNN-<noun>.md`) — one hard-to-reverse decision.
  Immutable once accepted — supersede, never edit. Pick NNN with
  `python scripts/lint_adr_numbers.py --next`.
- A feature is documented when the owning PRD covers its requirement, the
  owning spec covers its design, and any hard-to-reverse decision has an
  ADR. Most features amend existing documents.
- Every doc lives in exactly one `docs/` kind folder — never loose at the
  docs root, never in a new subfolder without prior agreement. Start from
  `docs/templates/`. Filenames: lowercase-kebab, generic English nouns.
  The names `requirements/` and `tech-specs/` are retired portfolio-wide.
- **Enforcement:** `axi hygiene stat docs` audits any repo against the
  standard; TIDY runs it in the heartbeat sweep (see the `doc-hygiene`
  skill) and proposes fixes through propose → approve.

Formatting:

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

### What the pre-push gate cannot see

A green pre-push run does not mean a green CI run, and the gap is structural
rather than a matter of being thorough.

`pyproject.toml` sets `addopts = "-m 'not integration and not
federation_lifecycle and not install_path and not classroom_e2e'"`, and the
hook mirrors that selection exactly. So four classes of test are deselected on
every developer machine — running them locally reports `deselected`, not
`passed`. Integration, Migration and Install-Path then ran only on `main` or a
tag, which is to say *after* merge.

That is how sixteen consecutive CI failures landed on main between 25 and 28
September, all in three RAG integration tests that no contributor could have
run and no pull request could have exercised.

Two things follow for anyone pushing:

- A green hook proves the unit suite. It proves nothing about integration,
  migration, install-path or classroom behaviour.
- Those suites now also run in the **merge queue** (`merge_group`), so they
  gate at merge rather than after it. A PR that passes everything visible to it
  can still be rejected at the queue, and that rejection is the system working.

The platform-level fix is not "run everything locally" — the suite is deselected
for good reasons, including cost and external dependencies. It is to know which
gate proves what, and to read a merge-queue failure as information rather than
as a flake.

### Pushing onto red main: override-reason required

Install the repo hooks once: `cp scripts/hooks/pre-push scripts/hooks/commit-msg scripts/hooks/post-merge .git/hooks/ && chmod +x .git/hooks/pre-push .git/hooks/commit-msg .git/hooks/post-merge`. (`post-merge` is advisory: after a pull it surfaces development lanes that became reclaimable, proposing `axi lane release` / `dropdb` and running nothing. Silence it with `AXI_NO_RECLAIM_HINT=1`.)

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
