# ADR-008: WebAssembly Runtime for Surrogate Model Extensions

**Status:** Proposed (Pending Spike Results)

**Date:** 2026-01-27

**Authors:** Benjamin Booth

**Deciders:** [TBD based on spike outcome]

---

## Context

Axiom requires a mechanism to execute surrogate models (ML approximations of expensive physics codes) in a way that is:

1. **Auditable** — Regulators need reproducible execution for regulatory body compliance
2. **Secure** — Models should not have ambient authority to access system resources
3. **Performant** — <10ms inference latency for real-time digital twin updates
4. **Polyglot** — Support models written in C/C++, Rust, and potentially Mojo

Current approaches in domain-specific digital twin platforms treat surrogate models as trusted code running in the same process as the host application. This creates several problems:

- **Security:** A malicious or buggy model can access filesystem, network, memory
- **Reproducibility:** Native code may produce different results on different platforms
- **Auditability:** No isolation boundary for logging and replay
- **Versioning:** Model updates require full application redeployment

### Alternatives Considered

| Approach | Pros | Cons |
|----------|------|------|
| **Native code (current)** | Maximum performance | No isolation, non-deterministic, security risk |
| **Docker containers** | Strong isolation | ~100ms startup overhead, resource heavy |
| **gRPC microservices** | Language agnostic | Network latency, operational complexity |
| **Python subprocess** | Easy integration | GIL, slow startup, memory overhead |
| **Lua embedding** | Fast, sandboxed | Limited ecosystem, not designed for numerics |
| **WebAssembly (WASM)** | Near-native perf, sandboxed, deterministic | Newer technology, limited GPU support |

---

## Decision

**Adopt WebAssembly (WASM) with Wasmtime as the runtime for surrogate model execution.**

Specifically:
- Use **Wasmtime** (Bytecode Alliance) as the WASM runtime
- Implement host in **Rust** for performance and safety
- Define model interface using **WIT** (WebAssembly Interface Types)
- Compile surrogates from C/C++/Rust using **WASI SDK**
- Provide Python orchestration layer for data science workflows

---

## Rationale

### Why WASM?

1. **Deterministic Execution**
   - WASM semantics are fully specified (unlike C undefined behavior)
   - With NaN canonicalization and disabled relaxed SIMD, results are bit-exact
   - Same binary produces same output on macOS, Linux, Windows, ARM, x86

2. **Capability-Based Security**
   - WASI provides explicit capability grants (filesystem, network, clock)
   - No ambient authority — models can only access what host explicitly provides
   - Resource limits (memory, fuel/instructions) prevent runaway execution

3. **Near-Native Performance**
   - Wasmtime achieves 0.8-1.2x native speed with AOT compilation
   - Cold start ~5-50ms (acceptable for our use case)
   - Warm inference meets <10ms target

4. **Multi-Language Support**
   - C/C++ via WASI SDK (clang)
   - Rust has first-class WASM target
   - Mojo targeting WASM (announced, coming 2026-2027)
   - Python via componentize-py (with caveats)

5. **Audit Trail Integration**
   - Every model invocation can be logged with inputs, outputs, timing
   - Determinism enables exact replay for regulatory review
   - Model binaries are immutable, versioned artifacts

### Why Wasmtime?

- **Bytecode Alliance** backing (Mozilla, Fastly, Intel, Microsoft)
- **Production-proven** (Fastly, Shopify, Fermyon)
- **Active development** of Component Model
- **Rust-native** for tight integration with our host

### Why Not Alternatives?

- **Docker:** Startup overhead too high for <10ms latency target
- **gRPC:** Network serialization adds latency, operational complexity
- **Native:** Cannot provide security or determinism guarantees
- **V8/Node:** Not designed for embedded use, larger footprint

---

## Consequences

### Positive

- **Regulatory pathway:** Can demonstrate reproducible execution to regulatory body
- **Security isolation:** Surrogate models cannot access unauthorized resources
- **Version control:** Model binaries are hashable, immutable artifacts
- **Language flexibility:** Teams can write surrogates in preferred language
- **Testing:** Determinism simplifies testing and CI/CD

### Negative

- **Learning curve:** Team needs to learn WASM toolchain
- **No GPU:** WASM has no GPU access; heavy inference must pre-compute
- **Component Model immaturity:** WIT and components are still stabilizing
- **Debugging:** WASM debugging tools less mature than native

### Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Performance doesn't meet <10ms | Low | High | Spike will validate; fallback to native for critical path |
| Component Model changes | Medium | Medium | Pin Wasmtime version, update incrementally |
| Mojo WASM delayed | Medium | Low | C/Rust sufficient; Mojo is optimization, not requirement |
| Determinism edge cases | Low | High | Extensive cross-platform testing in spike |

---

## Validation Criteria (Spike)

Before finalizing this ADR, the spike at `spikes/wasm-surrogate-runtime/` must demonstrate:

- [ ] Cold start < 50ms
- [ ] Warm inference < 10ms for GP surrogate
- [ ] Bit-exact results across macOS (ARM) and Linux (x86)
- [ ] Memory limits enforced (OOM → graceful error)
- [ ] Fuel limits enforced (timeout → graceful error)
- [ ] WIT interface supports predict/validate/metadata pattern

---

## Implementation Plan

### Phase 1: Spike (2 weeks)
- Implement Rust host with Wasmtime
- Compile C GP surrogate to WASM
- Benchmark and document findings
- Update this ADR with results

### Phase 2: Integration (4 weeks)
- Integrate with Axiom model registry
- Implement audit logging
- Add Python client library
- Documentation and examples

### Phase 3: Production (Ongoing)
- Convert existing surrogates to WASM
- Performance optimization
- GPU fallback for large models

---

## Related Decisions

- **ADR-001:** Polyglot monorepo (Bazel build)
- **ADR-006:** MCP server for agentic access
- **ADR-007:** Streaming-first architecture

---

## References

1. [WebAssembly Specification](https://webassembly.github.io/spec/)
2. [Wasmtime Documentation](https://docs.wasmtime.dev/)
3. [WASI Specification](https://wasi.dev/)
4. [Component Model](https://component-model.bytecodealliance.org/)
5. [Deterministic WASM](https://github.com/aspect-build/rules_wasm#determinism)
6. Spike implementation: `spikes/wasm-surrogate-runtime/`
7. Research paper: `docs/research/papers/2026-wasm-domain-specific-surrogates/`
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
