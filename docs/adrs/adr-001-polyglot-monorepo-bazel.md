# ADR-001: Polyglot Monorepo with Bazel Build System

**Status:** Proposed  
**Date:** 2026-01-14  
**Decision Makers:** Ben, Team

## Context

Axiom needs to support multiple programming languages:
- **Python** - Data pipelines, ML models, orchestration (Dagster, dbt)
- **TypeScript** - Frontend (React + Vite), API clients
- **Go** - Hyperledger Fabric chaincode
- **C** - Real-time data acquisition (future)
- **Mojo** - High-performance compute (future)

We need a build system that can:
1. Build all languages from a single command
2. Cache builds across CI and local development
3. Manage cross-language dependencies
4. Scale as the codebase grows

## Decision

We will use **Bazel** as the unified build system for the Axiom monorepo.

## Alternatives Considered

| Tool | Pros | Cons | Verdict |
|------|------|------|---------|
| **Bazel** | True polyglot, hermetic, remote caching, Google-scale proven | Steep learning curve, verbose BUILD files | ✅ Selected |
| **Pants** | Python-first, easier setup, good caching | Less mature for C/Go, smaller community | ❌ |
| **Nx** | Excellent for JS/TS, easy setup | Weak Python support, no C/Go | ❌ |
| **Make** | Universal, simple | No caching, no dependency graph, manual | ❌ |
| **Per-language tools** | Familiar (pip, npm, go mod) | No unified builds, duplication | ❌ |

## Consequences

### Positive
- Single `bazel build //...` builds everything
- Remote caching reduces CI times significantly
- Hermetic builds ensure reproducibility
- Language-agnostic dependency graph
- First-class support for all target languages

### Negative
- Team needs to learn Bazel concepts (WORKSPACE, BUILD, rules)
- Initial setup requires writing BUILD files
- Some Python tooling (editable installs) requires workarounds
- Verbose configuration compared to native tools

### Mitigations
- Provide example BUILD files for common patterns
- Use `rules_python`, `rules_nodejs`, `rules_go` rulesets
- Document Bazel workflows in `docs/development/`
- Consider `gazelle` for auto-generating BUILD files

## Implementation

```
monorepo/
├── WORKSPACE              # External dependencies
├── BUILD.bazel            # Root build file
├── .bazelrc               # Build configuration
├── .bazelversion          # Pin Bazel version
├── packages/
│   ├── python/
│   │   └── BUILD.bazel    # py_library, py_test targets
│   └── typescript/
│       └── BUILD.bazel    # ts_library, npm targets
└── tools/
    └── BUILD.bazel
```

## References

- [Bazel Python Rules](https://github.com/bazelbuild/rules_python)
- [Bazel Node.js Rules](https://github.com/aspect-build/rules_js)
- [Bazel Go Rules](https://github.com/bazelbuild/rules_go)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
