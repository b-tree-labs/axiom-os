# ADR-039: Scientific Displays — Math, Deterministic Compute, Auto-Charts, Federation Share

**Status:** Proposed (2026-05-01)
**Supersedes:** none
**Related:** ADR-022/023/024/025 (federation), ADR-026 (ownership), ADR-027 (federated memory), ADR-028 (trust graph), ADR-029 (federation composition), ADR-030 (federated inference), ADR-031 (extension self-containment), ADR-035 (LLM-tier policy), ADR-036 (extension runtime surfaces), ADR-037 (federation state propagation), ADR-038 (in flight — built-in MCP)
**Specs:** `spec-aeos-0.1.md`, `spec-model-routing.md`, `spec-event-bus.md`, `spec-classification-boundary.md`, `spec-brand-identity.md`, `spec-scientific-displays.md` (this capability)
**PRD:** `prd-scientific-displays.md`

## Context

Axiom is positioning to be the preferred harness for deep-science work across any domain. Federation, EC/classified routing, and unified composition memory are the asymmetric edges that already exist. Scientific Displays is the next edge: rendering math at Mathematica-quality, computing deterministically (never hallucinated), and producing peer-shareable signed scientific artifacts that survive the federation. Today, scientists leave the harness at every hand-off (chat → Mathematica → Jupyter → matplotlib → screenshot → Slack), losing state and provenance at each step.

The capability has three pillars (math rendering, deterministic + federation-routed computation, auto-charts with peer-share) and one centerpiece (the closed loop: LLM proposes → SymPy verifies → provenance composes → figure signs → federation distributes). The PRD is `prd-scientific-displays.md`; the technical spec is `spec-scientific-displays.md`. This ADR records the load-bearing architectural decisions.

This ADR is intentionally domain-agnostic per `feedback_axiom_domain_agnostic` and the project CLAUDE.md: no nuclear / reactor / facility / course references. The capability serves any deep-science domain that touches math, compute, and figures.

## Decision

### D1 — Multi-modal input; LaTeX is primary; everything compiles to a SymPy AST

Math input accepts four modalities: LaTeX (primary), AsciiMath (alternative), Unicode quick-codes (`\alpha → α` deterministic substitution), and a `/math` slash command opening a multi-line editor with live preview. All four converge on a SymPy AST (`sympy.Expr`) before render, compute, or provenance touches the data.

Rationale:

- LaTeX is the universal scientific mathematical interlingua; making it primary is non-negotiable.
- AsciiMath is the accessibility offer for users who haven't learned LaTeX; lower-barrier entry.
- Quick-codes serve the chat-typing case where pulling up `$...$` is friction.
- A single canonical AST is what makes the rest of the system (compute, provenance, sharing) tractable. Without it, we're maintaining N parsers × M downstream consumers.

Rejected alternatives:

- LaTeX-only — too high a barrier for instructors and students who haven't internalized it.
- ASCII-improvise (the current state of most chat tools, where the model writes `≈ a/b`) — explicitly the failure mode this capability eliminates.

### D2 — Deterministic kernel authorizes computation; LLM proposes only

The LLM may propose an equation, name a likely answer, or suggest a method. It does **not** authorize a numerical or symbolic result. Every numerical result in a `result_block` carries a SymPy provenance hash; CI fails the release if any `result_block` is missing one (success metric M3 in the PRD).

Rationale:

- This is the asymmetric edge. "We don't hallucinate the math" is the ground truth claim that distinguishes this from every other agentic harness.
- The AEOS "deterministic trust boundary" principle (§3.4) makes this an architectural invariant, not a stylistic choice.
- Provenance composes cleanly with the existing CompositionService: the result_block is just a typed MemoryFragment with `(T, U, A, R)` populated.

Consequences:

- Compute that SymPy/NumPy/SciPy/mpmath cannot do is exposed as "couldn't compute symbolically" with the equation still rendered. The harness never improvises a number.
- Some user workflows that "just want a quick estimate" will feel slow; the answer is to make the deterministic path fast (cache, lambdify, federation-route), not to relax the invariant.

### D3 — Sandbox model: subprocess + OS-level confinement (Seatbelt on macOS, seccomp/unshare on Linux); container is opt-in, not default

Computation runs in a subprocess with `RLIMIT_AS` + `RLIMIT_CPU` + minimal `RLIMIT_NOFILE`, no inherited file descriptors, no inherited environment beyond an allow-list, no network (`unshare --net` on Linux; `sandbox-exec` deny-all-network profile on macOS). Containers (`docker run`) are an opt-in `--container` flag for users who already have Docker; not the default.

Rationale:

- Subprocess isolation is available everywhere Python runs (laptop, workstation, server, platform). Container-as-default would block laptop users who don't have Docker — a Prague-blocker.
- Seatbelt + seccomp provide meaningful defense-in-depth without an extra runtime dependency.
- The threat model is mostly "the user (or LLM) accidentally constructs an expression that opens a file or hangs the CPU" — subprocess + rlimits + no-network handles that. Adversarial-LLM-with-supply-chain-compromise is the AEOS attestation surface (§9.4), not the sandbox surface.

Rejected alternatives:

- Container-by-default — too heavy for the laptop case; defers Prague.
- In-process (no subprocess) — one bad expression hangs the chat process; unacceptable.
- eBPF-only — Linux-only; we can't ship a macOS regression.

Future: we may revisit gVisor or Firecracker for the federation-peer compute case where the executing node is an untrusted but compute-rich peer; that's a Phase D consideration.

### D4 — Federation routing of long compute is **pre-emptive** (estimate-then-route), not post-fail fallback

A cheap deterministic pre-flight estimator (matrix dimension, expression node count, dsolve order, etc.) classifies expected runtime into `fast (<2s) | medium (2–30s) | long (>30s)`. Long jobs trigger the federation routing dialog *before* the local job runs. Medium jobs run locally with the background-tasks placeholder. Fast jobs run inline.

Rationale:

- Post-fail fallback is wasteful: the user's laptop spins up the eigendecomposition, runs for 90 seconds, fails the deadline, and *then* we ask if they want to try the workstation. The user's battery and the user's patience are both already spent.
- Pre-emptive lets the user catch surprising classifications — if the estimator says "long" on something they expected to be fast, that's a signal worth surfacing before they wait.
- The estimator is a heuristic, not an oracle; it can be wrong. When wrong-low (we said "fast", it took 5 minutes), the background-tasks primitive catches the user with a placeholder; when wrong-high (we said "long" when it would have been 2s), the user waved off federation and ran locally — no harm done.

Rejected alternatives:

- Post-fail fallback — see above.
- LLM-classified routing — the routing decision is too high-stakes (cost, latency, classification routing) to delegate to the LLM. Deterministic estimator only.
- Always-route-to-strongest-peer — burns the trust budget of peers who agreed to advertise compute capability; cohort federation will cease to be cooperative.

### D5 — Auto-chart selector is **deterministic with optional LLM tiebreak**, never LLM-primary

The chart selector evaluates a declared rule table top-to-bottom; first match wins. The fired rule name is recorded in `ChartRender.rule_fired` and surfaced to the user (transparency requirement, PRD M10). When no rule matches AND no explicit intent is given AND `LLM_TIEBREAK_ENABLED` is true (default true on Workstation+; false on Edge), a `simple` LLM tier (per ADR-035) chooses from the closed candidate set. The LLM never invents a chart type.

Rationale:

- "Always pie" tools fail because LLMs pick the chart type based on prompt vibes, not data shape.
- Deterministic rules are testable, debuggable, override-able, and explainable. Per-domain extensions can ship policy files that override.
- LLM-primary makes the chart type a function of the model and the prompt phrasing — non-reproducible across sessions.
- LLM tiebreak preserves the rule-first invariant while gracefully handling the long tail of "data shape doesn't fit any rule and the user gave no hint."

Rejected alternatives:

- Pure LLM — opaque, untestable, non-reproducible.
- Pure deterministic with no fallback — produces "table" (or refuses) too often when the rule set is incomplete; bad UX.
- Per-rule confidence weighting → top-K → LLM choose — over-engineered for the real-world frequency of true ambiguity.

### D6 — Federation-hosted sharing is the *default* `/share` backend; signed by producer; cohort-scoped by default

`/share` defaults to `share_federation` backend. Artifacts are signed by the producer's node keypair (per ADR-026), cohort-scoped (defaults to current chat session's cohort), 30d expiry default. Cross-cohort visibility requires `--public`. Other backends (`local`, `s3`, `seaweedfs`) are configurable but not default.

Rationale:

- Federation-hosted *is* the asymmetric edge of the share story. Defaulting to it makes the edge visible from day one. If the default were `local`, users would never discover the federation path.
- Signing + cohort-scope + expiry are conservative defaults: a casually-shared figure does not become a permanent public asset by accident.
- Re-using the ADR-037 federation directory with new typed records (`SHARED_ARTIFACT`, `REVOKED_SHARED_ARTIFACT`) avoids inventing a parallel registry, parallel revocation channel, or parallel signing scheme.

Consequences:

- Single-user installs that have not joined any cohort fall back to `share_local` automatically (with an inline notice). This keeps `/share` working out-of-the-box without requiring federation setup.
- Revocation works through the same gossip primitive; honors the existing trust graph.

Rejected alternatives:

- `local` default — invisible asymmetric edge; users never discover federation share.
- `s3` default — requires S3 setup; not portable.
- `--public` default — accidentally exposes work outside the cohort; bad safety default.

### D7 — Pluggable share backends via the `ShareBackend` Protocol; not a registry-per-backend

A single `ShareBackend` Protocol with `publish / resolve / revoke`. Backends register via Python entry points (`axiom.scidisplay.share_backends`). The selector picks the backend by name from settings or the `--backend` flag.

Rationale:

- Allows an org to plug in their own backend (Artifactory, Azure Blob, internal CDN) without modifying the extension.
- Keeps the surface narrow: three methods, one protocol.
- Mirrors the existing extension-runtime-surfaces pattern (ADR-036) — capabilities discover their backends, they don't hard-code them.

Rejected alternatives:

- Hard-coded backends — extensibility tax forever.
- One-class-per-backend with abstract base — Python protocols are lighter and don't impose the inheritance.

### D8 — Capability detection is per-chat-session, cached, and observable

Terminal capability (image protocol, Unicode coverage, font availability, color depth, ssh/tmux state) is probed once at chat-session start and cached on the session. The result is observable via `axi sci diag` so users can see what the harness believes about their terminal.

Rationale:

- Probing per render is wasteful and noisy (escape sequences echo to the screen).
- A user ssh-ing into a different terminal mid-session is a corner case; document the workaround (`/refresh-display-capability` slash command).
- Observability of the probe result is a debuggability win when a render falls back unexpectedly — "why is my equation in plain text?" → `axi sci diag` shows the probe inferred no image protocol.

### D9 — Mirror existing in-chat artifact patterns; do not invent a new render-cache surface

The math + chart inline-render path uses the same on-disk structure, content-addressed caching, subprocess-with-timeout discipline, placeholder-line format, and open-file helper that `chat/fullscreen.py` already uses for Mermaid. If divergence becomes warranted, the Mermaid path is migrated to the unified `infra/sci_render/inline_artifact.py` in the same change — never two parallel half-migrations.

Rationale:

- Two independently-evolving inline-artifact surfaces is the kind of duplication that quietly grows surface area for years.
- The Mermaid path is already battle-tested for the chat surface; reusing the contract gets us velocity for free.
- Per `feedback_no_backward_compat_shims`: clean refactors over compat shims. If we lift the pattern, we lift it once and migrate Mermaid in the same commit set.

### D10 — AEOS conformance: Bronze in Phase A, Silver in Phase B, Gold in Phase C

Conformance level rises with the capability:

- **Phase A — Bronze.** Manifest validates, layout conforms, Tier 1 + Tier 2 CLI commands work. No federation features yet.
- **Phase B — Silver.** Sigstore-signed releases, ≥85% test coverage, `__all__` enforced via import-linter.
- **Phase C — Gold.** Behavioral attestation supported, quarantine recoverable, classification ceiling declared, Tier 4 CLI commands operate (federate, attest, quarantine, recover).

Rationale:

- The conformance rise tracks the actual federation surface coming online. There's no point claiming Gold before federation share + routing exist.
- Sets the bar without over-burdening early-phase shipping.

### D12 — Code rendering is a Pillar 1 capability, not a sidecar

Scientific Displays renders code with the same priority as math. The Phase A bar is "looks better than any other terminal-based agentic harness" — Pygments through Rich's `Syntax` widget, three Axiom-branded themes (`axiom-dark` / `axiom-light` / `axiom-high-contrast`) anchored to the brand palette (graphite + off-white + UT burnt-orange), language-badge + line-gutter, ligature-font advisory on first encounter (advisory, not enforcement). Phase B upgrades to tree-sitter semantic parsing for the top 20 languages and adds signed code-share receipts (formatter + linter + typecheck attestations).

Rationale:

- Code rendering is **table stakes** for any agentic harness that handles deep-science work; without it, the harness fails the first 30 seconds of the demo. Putting it in Pillar 1 alongside math (rather than as a Phase D polish item) reflects that bar.
- Pygments-default in Phase A keeps the surface contract small and the implementation tight — Rich already ships a battle-tested `Syntax` widget; we provide the themes + lexer-selection priority + integration glue, not a from-scratch renderer.
- Tree-sitter in Phase B is the asymmetric move. Helix demonstrated semantic highlighting is a visible step-change beyond regex-Pygments — variable/definition distinction, scope-aware coloring, type-annotation dimming. No other agentic harness ships this today (Aider, Codex, Cursor TUI, Continue, Cline all use Pygments-class rendering).
- Code-share receipts in Phase B close the asymmetric loop for code the same way provenance receipts close it for math: peers see *attested* code (linter + formatter + typecheck pass status), not just pasted code. The attestation travels with the artifact through the federation.
- Ligature-font handling is **advisory not enforcement** — we never refuse to render because the font isn't ideal. The one-line tip on first encounter sets the user up for the best experience without gating; suppression is one slash command away.
- Themes publish as standard Pygments style classes via `entry_points` group `pygments.styles` so a user can `pip install axiom-scidisplay` and reference `axiom-dark` from `bat`, GitHub Codespaces, IPython, or any other Pygments consumer. Brand consistency across surfaces is a feature, not coincidence.

Rejected alternatives:

- **Ship our own from-scratch lexer.** Rejected — Pygments has 500+ language lexers, decades of maintenance, and Rich already wraps it well. Reinventing this is pure cost with no asymmetric upside.
- **Skip Pygments and go straight to tree-sitter.** Rejected — tree-sitter requires a per-language grammar binary and adds non-trivial install complexity. Pygments-default ensures Phase A ships fast and works everywhere; tree-sitter is a strict upgrade for users who opt in.
- **Bundle a font.** Rejected — font licensing is a quagmire (JetBrains Mono is OFL but bundling adds 10MB to the wheel; user-system fonts are how every other tool handles this). Advisory-only respects the user's font choices.

### D11 — Phase A ships the whole asymmetric loop, not just rendering

Pillar 1 in Phase A originally scoped to "render only" — compute kernel, `/math` editor, and MathJax browser fallback were deferred to Phase B. This decision (2026-05-01 update) collapses that boundary: **A6 (compute kernel), A7 (`/math` editor), A8 (MathJax browser fallback)** are promoted into Phase A.

Rationale:

- The asymmetric edge is the **closed loop** ("LLM proposes, kernel verifies, signed receipt"), not the rendering. Shipping rendering without compute makes Axiom indistinguishable from any LaTeX renderer; the demo loses its bite, and Phase A delivers no real differentiation against ChatGPT/Claude/Cursor pasted-LaTeX-and-handwave.
- The `/math` editor is the primary input modality for any equation longer than a single line. Without it, Phase A users are forced through the regular chat input — a degraded experience that mis-positions the capability.
- MathJax browser fallback is needed on day one because instructor authoring (a primary persona) frequently happens in browser sessions before terminal verification. Holding browser parity for Phase B would force instructors to a degraded path during the most-visible window (Prague + immediately after).
- The cost is real: compute kernel + sandbox is significant additional engineering for the pre-rehearsal buffer. Phase A remains explicitly a *stretch* against Prague (it always was — even the rendering-only scope was stretch). The compensation is that if Phase A lands at all, what lands is the actual asymmetric edge, not a half-edge that requires "wait for Phase B" caveats every time it's demoed.

Rejected alternative:

- **Keep "Phase A = rendering only" and demo deferred capabilities as roadmap items.** Rejected because the asymmetric value proposition collapses to "we render LaTeX nicely," which is true of every existing tool. The strategic point of this work is *not* prettier math — it's the closed loop. Ship the loop or ship nothing.

Phase B becomes the **provenance + backgrounding + alternate-input** phase: AsciiMath input, full signed `(T, U, A, R)` receipts, and background-job protocol integration with the Coordinator. Phase B is no longer "the closed loop arrives" — Phase A delivered that. Phase B hardens it.

## Consequences

### Positive

- The harness becomes the preferred environment for deep-science work; users stop leaving for Mathematica/Jupyter/matplotlib for the common case.
- Federation gains a second compelling artifact type (figures, after memory fragments) — the network effect of the trust graph extends to the work product, not just the metadata.
- The asymmetric edge is concrete and demoable: type an integral, get a typeset equation + a deterministic answer + a signed shareable receipt — in iTerm2, in 5 seconds, with one keystroke to share to a colleague at another institution.
- The closed-loop architecture is reusable. A future "scientific report" extension reuses the math + chart + provenance primitives from `infra/sci_render/`.

### Negative

- New capability surface to maintain: math parsers, render paths, sandbox, federation share. The build is real.
- matplotlib is great-but-not-Mathematica for math typography; we ship with that gap and document it (PRD R6).
- Image-protocol fragmentation is a permanent test-matrix tax (iTerm2, Kitty, WezTerm, Ghostty, Sixel, none).
- Pre-emptive routing requires a working federation directory (ADR-037) — Phase C depends on Phase 1+ of ADR-037 shipping.

### Honest about Prague

- Pillar 1 subset (Phase A — math rendering, plain-terminal fallback, quick-codes, inline chat hook) is *stretch* for the Prague rehearsal buffer (2026-05-22). It is not Prague-critical.
- Pillars 2 and 3 (Phases B + C) are post-Prague.
- This ordering is correct: Prague's blockers are Keplo + classroom + federation seeding, not scientific displays. Don't pull this in front of those.

## Compliance with Project Conventions

- Domain-agnostic: ✅ no nuclear / reactor / facility / course-name references in any module, doc, or default.
- AEOS-conformant: ✅ designed against `spec-aeos-0.1.md` §5 layout from day one.
- Mermaid-only diagrams: ✅ all diagrams in PRD + spec are Mermaid; vertical TD/TB layout; every node + subgraph styled with `fill:` + `color:`.
- TDD: ✅ the spec defines the test matrix per phase before any code is written.
- Phased delivery ships value per phase: ✅ Phase A delivers usable math rendering even with no compute / no charts / no sharing.
- No back-compat shims: ✅ greenfield extension; no legacy surface.
- Hold pushes: ✅ branch-only commits; no push to remote.

## Open Questions (carry forward, not blockers)

- **OQ1.** Is `theme_light` the right default for figure export to docs/papers? PRD OQ6 carries this; revisit after first instructor uses it.
- **OQ2.** Should `compute` results display a "verified" badge? Spec OQ7 carries this; defaulting to a neutral provenance footer for v0.1.
- **OQ3.** Will an Axiom MathJax build need to live somewhere centrally so the post-Prague web surface and any future docs site share it? Out of scope for v0.1; revisit when web surface starts.

---

_Copyright (c) 2026 B-Tree Ventures, LLC. Apache-2.0 licensed._
