# ADR-102 — Builder runbooks: one executable source, four projected surfaces

- **Status:** Accepted
- **Date:** 2026-08-13
- **Deciders:** Platform
- **Related:** ADR-056 (CLI verbs are thin wrappers over skill functions), the
  "one gold verb, four surfaces" data pattern, spec-aeos-0.1 (extension layout).

## Context

A *builder-oriented feature* — one a person extends or authors against (writing
a monitor, a connector, a verb) rather than merely consuming — needs a
step-by-step guide: how to go from nothing to a working, deployed thing. Today
those guides are loose markdown scattered across repos, and the same steps drift
between the docs, the CLI `--help`, and what the AI chat says.

We already solved the *data* version of this problem. A gold verb is
simultaneously a SQL function, a CLI verb, an MCP tool, and a notebook cell —
with a traditional web view and a mobile app as the fifth and sixth surfaces —
**authored once, projected to every surface, never drifts.** Procedural
knowledge (runbooks) has no equivalent discipline, so it rots.

Prior attempts at a standalone documentation site were repeatedly abandoned: a
separate site is a *second content store* that drifts from the code, needs its
own publishing pipeline, and is invisible from the terminal and the assistant
where builders actually work.

## Decision

**A builder feature owns exactly one executable runbook, and every other
surface is a projection of it.** This is the procedural twin of the gold-verb
pattern.

### 1. Single source — an executable runbook that ships in the package

The runbook is one markdown file at `<extension>/docs/runbook-<slug>.md`,
shipped with the package (like a client-API reference already is). It
interleaves prose with runnable commands. It is authored **jupytext-compatible**
(fenced code cells, no exotic markup) so it doubles as a notebook without a
second copy. This file is the source of truth; the surfaces below are views.

It carries a frontmatter block declaring which surfaces it feeds and its
promotion phases:

```yaml
---
runbook: build-a-monitor
audience: builder
surfaces: [package, cli, chat, notebook]
phases: [local, stage, prod]
---
```

### 2. Four surfaces, all projections of that one file

| Surface | How | Rule |
|---|---|---|
| **Docs** | ships in the package; optional read-only mkdocs render of the in-repo docs | **No standalone content store.** Docs version, ship, and deploy with the code. A rendered site, if any, is generated from the source — never authored separately. |
| **CLI** | a `guide` verb prints/opens the shipped runbook; its steps *are* real verbs | Extends the existing `axi ext quickstart` pattern. The CLI is both the guide and the tools the guide invokes. |
| **AI chat** | the runbook is indexed in a **distinct builder-docs RAG lane** (`doc_class='builder'`), separate from the operational/data corpus | Builder how-to must not pollute data grounding, and vice-versa. Because the chat's tools already include the CLI verbs, the agent can *execute* the runbook, not just recite it — and RAGs the same shipped file, so answers can't go stale. |
| **Notebook** | the same jupytext markdown opens as a runnable `.ipynb` | The "runbook that runs." Foundation first (jupytext round-trip + executable cells); the full Verb/Run/Ask/Explore cell model layers on later. |

### 3. A fixed local → stage → prod spine

Every runbook is structured around the same three promotion phases, each mapped
to a real gate:

- **Local** (your machine): install → configure → write → test → back-test.
  Pure, read-only credential, no deploy.
- **Stage** (prove it): PR → CI test ladder → rehearsal against the live
  endpoint → review. The gate between "works on my laptop" and shared.
- **Prod** (promote): tag → deploy → runs for everyone → monitored.

This is not new process — it is the promotion path the team already follows;
the ADR names and templates it so every builder feature exposes it identically.

## Consequences

- **One edit updates every surface.** Fixing a step in the runbook fixes the
  docs, the CLI `guide`, and the chat's answer at once.
- **No docs-site drift.** The recurring failure mode (a second content store) is
  designed out; a browsable render, when wanted, is a generated view.
- **The chat can act, not just explain**, because builder docs and the tools
  they describe are the same CLI surface.
- **Notebook-ready for free**, because the source is authored jupytext-clean.
- **Scope discipline:** ship the template + one reference instance (the monitor
  quickstart) first. Generalize into a runbook registry only when a *third*
  builder feature needs it — two instances is a coincidence, three is a pattern.

## Reference instance

`ut-triga-site/docs/quickstart-build-a-monitor.md` — the monitor-author
quickstart — is the first runbook conforming to this ADR. The template lives at
`docs/reference/runbook-template.md`.
