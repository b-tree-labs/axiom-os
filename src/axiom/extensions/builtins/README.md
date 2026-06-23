# `builtins/` — Domain-Agnostic Builtin Extensions

Each subdirectory is a self-contained extension that ships with the platform.
These are **domain-agnostic** — they work for any deployment without
customization.

## Naming Convention

**Extensions are named by purpose, with no type suffix** (per AEOS §5.4):
`signals/`, `publishing/`, `diagnostics/`. The capability kind — `agent`,
`tool`, `cmd`, `service`, `adapter`, `skill`, `hook` — lives in the
`axiom-extension.toml` manifest, not the directory name. Plural names go to
streams or collections (`signals/`, `agents/`); singular to activities and
states (`chat/`, `publishing/`, `hygiene/`).

A few representative extensions:

| Directory | Description |
|-----------|-------------|
| `signals/` | Signal ingestion, extraction, synthesis (hosts SCAN) |
| `chat/` | Interactive LLM assistant (hosts AXI; consumer layers rebrand) |
| `hygiene/` | Resource stewardship and system hygiene (hosts TIDY) |
| `diagnostics/` | AI-powered diagnostics and self-healing (hosts TRIAGE) |
| `publishing/` | Document lifecycle (md → docx → publish, hosts PRESS) |
| `rag/` | Retrieval corpus management (ingest, search, audit) |
| `data_platform/` | Bronze/Silver/Gold storage backends |

Discover the rest via `axi commands` or by reading the per-extension
`axiom-extension.toml` manifests.

## Extension Layout

Each extension follows this structure:
```
{name}/
  axiom-extension.toml  # REQUIRED — manifest
  cli.py                # CLI entry point (build_parser + main)
  tests/                # Colocated tests
  docs/                 # Extension-specific specs/docs
  infra/                # Dockerfiles, plist, deploy configs
  ...                   # Implementation files
```

## What belongs here

- New domain-agnostic extensions (useful to any deployment)
- Extensions that are part of the core Axiom experience

## What does NOT belong here

- **Domain-specific extensions** (industry-specific tools) →
  external repos, installed to `.axi/extensions/` or `~/.axi/extensions/`
- **Platform infrastructure** → `src/axiom/infra/`
- **Runtime data** → `runtime/`

## AI Agent Policy

When creating a new extension:
1. Pick a purpose-named directory — no type suffix (per AEOS §5.4)
2. Declare the capability kind(s) in the manifest: `agent` (LLM autonomy),
   `tool` (invoked capability), `cmd`, `service`, `adapter`, `skill`, `hook`
3. Create `{name}/axiom-extension.toml` with name, version, and a
   `[[extension.provides]]` block per capability
4. Create `{name}/cli.py` following the `build_parser()` + `main(argv)` pattern
5. Create `{name}/tests/` for colocated tests
6. Commands register from the manifest — the registry discovers each
   `[[extension.provides]] kind = "cmd"` entry, so no central dispatcher
   file needs editing

Never place loose Python files directly in `builtins/`. Every piece of
functionality must live inside a named extension subdirectory.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
