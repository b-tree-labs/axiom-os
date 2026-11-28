# ADR-286: Axiom is language-neutral at its edges and Python at its core

**Status:** Proposed (2026-11-24).
**Related:** ADR-285 (RIVET decides on content; no required external dependency), ADR-056 (CLI verbs are thin wrappers over skills), ADR-279 (one MCP surface with scoped profiles), ADR-170 and ADR-179 (blue/green, update with rollback), `docs/specs/spec-aeos-0.1.md`. It does not replace ADR-001 (a monorepo build system) or ADR-008 (a WebAssembly runtime for surrogate models), which decide other things. The owner is reconsidering the WebAssembly approach in ADR-008, so this ADR does not depend on it.

## Context

Axiom is a Python platform: extensions load through Python entry points, RIVET selects tests with a pytest-only tool, reads its version from `pyproject.toml`, lints with ruff and publishes to PyPI. The people Axiom serves keep code in many languages: JavaScript and TypeScript, Go, Rust, Java, Ruby, .NET, PHP, and C, C++ and Fortran for numerical work. A CI and release agent that only understands Python repos stops at the first adopter's door, and a platform that only hosts Python extensions cannot take a Go or Rust extension.

Two questions look alike and are not. One is which languages the repositories RIVET works on are written in. The other is which languages Axiom's own extensions and agents are written in. A third is how RIVET itself is delivered to a host that has no Python.

## Decision

- **Python at the core, neutral at the edges.** Identity, vault, memory, history and the skill registry stay in Python. Everything that crosses an edge uses a neutral contract: git, the forge's basic API, JUnit XML and CTest XML for test results, SARIF for findings, exit codes, lockfiles, SQL migrations, JSON for skill calls, MCP for tools.
- **An ecosystem profile, open-ended.** RIVET reads a language through a profile: data first, with optional code. A profile names, for one ecosystem and one path in a repo: how to detect it; its manifest and lock files; where the version lives; how to build and test; the result format; how to lint and fix; how to publish and attest; how to narrow tests; and what the repair catalog knows. A repository may have several profiles, chosen per directory. A build system that spans languages (Bazel, CMake, Nx) is itself a profile: where a repository uses one, its target graph is the best narrowing source for every language in it. That is independent of whether Axiom's own repository adopts Bazel, which ADR-001 leaves undecided. Adding an ecosystem is adding a profile and conformance fixtures, never a change to RIVET's core.
- **Three tiers, so no language blocks another.** Tier 1, judge only: from neutral inputs alone RIVET holds, lands, names flakes and reverts for any repository. Tier 2, profile: manifests, locks, version, publish. Tier 3, narrow and fix: per ecosystem and by demand; where no reliable selector exists RIVET runs the whole suite and the escape ledger reports the cost.
- **The contract carries four things beyond Python and JavaScript.** The environment (compiler, MPI, runtime and library versions, as a Spack environment, a container digest or a toolchain file) is a pinned input, and a change to it raises risk like a dependency major. Tests may be tolerance-aware against baseline data, and flake classification must tell a tolerance failure and a nondeterministic run from a real regression. A run may be a batch job submitted to a scheduler and polled, not executed inline. A controlled-code mode runs model-free and local-only for code that must not leave the machine.
- **Adapters run the adopter's own toolchain.** RIVET calls the project's own build and test commands and reads standard output formats with the standard library. Those tools are the adopter's, not Axiom's dependencies (ADR-285).
- **Extensions in other languages.** The manifest gains `runtime = "python" | "process"` and a command, with `wasm` reserved. A `process` extension is a subprocess speaking a language-neutral skill contract (JSON in, JSON out, with the manifest's existing risk, side-effect and idempotency metadata) over MCP stdio. WebAssembly stays an open option for sandboxed extensions, to be decided later on its own merits; this ADR commits only to `python` and `process`. Python entry points remain for Python extensions.
- **Delivery to hosts without Python.** If a thin client is needed, it is our own small static binary that collects JUnit, environment and timing and hands them to RIVET; it is built only when the tap rung proves the need.

## Consequences

- The ecosystem-profile contract, with fixtures, is the first deliverable. Profiles follow in an order the adopters' repositories set (the first-week backtest reports which ecosystems it finds).
- The version map (#2385) gains the environment spec; flake naming (#2236) gains tolerance and nondeterminism classes.
- A process-runtime contract for extensions and a controlled-code mode are separate issues.
- Tools that read language-specific output stay optional, behind the profile.

## Proof

- A conformance fixture per ecosystem: a tiny sample repository (JavaScript and TypeScript, Go, Rust, Java, a CMake-based C, C++ and Fortran project, and one each of Ruby, .NET and PHP). For each, RIVET detects it, classifies a lock-only change, reads the test result from the neutral format, and names a seeded flake, with no change to RIVET's core.
- A repository with two profiles resolves each directory to its own.
- A `process` extension written in a second language registers, is called through the skill contract, and its risk metadata gates it exactly as a Python skill's.
- In controlled-code mode a run makes no outbound call and the model-backed fixer is off.
- A batch job is submitted, polled and read back by the tap without running inline.
