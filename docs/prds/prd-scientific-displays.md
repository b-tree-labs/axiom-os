# PRD — Scientific Displays

**Product / Feature:** Scientific Displays — math rendering, code rendering, deterministic computation, auto-charts, and federation-shareable scientific artifacts inside the Axiom harness.

**Owner:** Ben Booth (B-Tree Labs)   •   **Status:** Draft (design)   •   **Last updated:** 2026-05-01

**Related ADRs:** ADR-027 (federated memory), ADR-029 (federation composition), ADR-030 (federated inference), ADR-031 (extension self-containment), ADR-035 (LLM-tier policy), ADR-036 (extension runtime surfaces), ADR-039 (this capability — *see `docs/adrs/adr-039-scientific-displays.md`*).

**Related specs:** `spec-aeos-0.1.md`, `spec-model-routing.md`, `spec-event-bus.md`, `spec-extension-layout.md`, `spec-brand-identity.md`.

---

## 1) Elevator Pitch

Scientific Displays makes Axiom the harness deep-science workflows do not have to leave: equations render with Mathematica-quality typography, computations run deterministically (with arbitrary-precision support) and route to peer compute when they get heavy, and charts auto-select the right type, render in the Axiom theme, and produce a single short URL that any peer in the federation can resolve to a signed, reproducible artifact.

## 2) Problem / Opportunity

Today every serious science workflow is a hand-off chain: ask the model in chat, copy the candidate equation into LaTeX, copy the LaTeX into Mathematica or Jupyter to actually compute it, copy the numerical result into matplotlib to plot it, screenshot the plot, paste it into a doc, walk back to chat for the next question. State is lost at every hand-off. Provenance is destroyed. The model regularly *hallucinates the computation* because it cannot run one. The "share with a colleague" step is a screenshot and a Slack message.

This is the dominant unsolved workflow problem for computational scientists, instructors who teach computation, and federation peers who want to inspect each other's work. Solving it inside Axiom turns the harness into the preferred environment for any deep-science domain — physics, chemistry, biology, materials, climate, computational engineering, mathematics — without naming any one of them in the product.

Axiom already has three asymmetric edges that compound here:

- **Federation** (ADR-022 through 037) — every artifact can travel signed across the trust graph; every long compute can route to a peer with the right hardware (`spec-model-routing.md`).
- **Memory composition** — every render, every computation, every figure carries an immutable `(T, U, A, R)` provenance tuple, so the closed loop is auditable.
- **Classification + EC routing** — the same boundary that prevents leaking export-controlled corpora to the cloud also prevents an export-controlled equation from being rendered on an unauthorized peer.

Scientific Displays is the fourth edge. It is the difference between "another chat" and "the harness deep science prefers to live in."

## 3) Vision

When a scientist opens `axi chat` from a laptop and asks about an integral, the equation is typeset in the terminal at SVG quality, SymPy verifies the closed form, the result is typeset alongside, both carry a provenance receipt, and a `/share` produces a short URL the colleague at the next institution resolves with a single click — no screenshots, no hand-offs, no hallucinated arithmetic, no platform that doesn't speak math.

Five years out, "I left Axiom to do the actual math" reads the way "I left my IDE to compile" reads now: a workflow we used to tolerate before tools were unified.

## 4) Personas

| Persona | Primary tasks | Technical level |
|---|---|---|
| **Computational scientist** | Derives equations, runs symbolic + numeric computations, generates publication figures, shares results with collaborators across institutions. | Expert: comfortable with LaTeX, Python, command line, occasional cluster jobs. |
| **Instructor (any domain)** | Authors example derivations live during class, generates problem-set figures, validates student-submitted derivations against the ground truth. | Expert in domain; intermediate in tooling. |
| **Student (any domain)** | Asks the harness to render derivations, follows along in their own session, submits derivations, receives feedback that includes typeset rebuttals and correct figures. | Novice to intermediate. |
| **Federation peer reviewer** | Receives a signed scientific artifact from a peer, verifies the derivation + computation + figure are reproducible from the embedded code + chunk references, comments back through the same channel. | Expert; cares about provenance. |

## 5) User Journeys

### Journey 1 — Pillar 1: render an equation with precision (instructor live in class)

1. Instructor types `\int_0^\infty e^{-x^2} \, dx` in chat (or `/math` to enter multi-line LaTeX with live preview).
2. Terminal capability is detected (iTerm2 image protocol available).
3. Equation is rendered as inline SVG-quality PNG, typeset with the precision matplotlib's mathtext + a font fallback to STIX provides.
4. Below the rendered equation: SymPy-computed closed form `\sqrt{\pi}/2`, also typeset.
5. A discreet provenance line: `Rendered by axiom-scidisplay 0.1.0 · SymPy 1.15 · 12 ms · receipt:axiom://...`.
6. If terminal is plain VT100, the same content renders via `sympy.printing.pretty` Unicode multi-line output (no broken ASCII fallbacks).

### Journey 2 — Pillar 2: deterministic + federation-routed computation (scientist on laptop)

1. Scientist asks: "diagonalize this 4096×4096 Hermitian matrix" (matrix attached).
2. Pre-flight cost estimate (cheap deterministic heuristic) flags this as `>30s expected`.
3. Routing layer (per `spec-model-routing.md`) consults peer capability records (`CAPABILITY` records from ADR-037 federation directory): laptop = ~2 min CPU; configured workstation peer (e.g. an HPC node the scientist's lab runs) = ~5 s.
4. Job is offered to the workstation peer; scientist confirms (one keystroke) or `--auto-route` was set.
5. Job submits to the federation peer's background-tasks runner (per `axiom.agents.background_service` Coordinator pattern), runs in a sandboxed subprocess with SciPy + NumPy + mpmath available.
6. Inline progress updates stream back over the federation event channel; cancellable with `/cancel`.
7. On completion, eigenvalues + eigenvectors render as a dataframe + heatmap inline; provenance receipt names the executing peer + cohort + signed result hash.

### Journey 3a — Pillar 1 (extended): code blocks that look better than any other harness

1. Scientist asks the agent to walk through a SciPy snippet. Reply contains a fenced ```python``` block.
2. Block is rendered with **tree-sitter** semantic highlighting (Phase B; Pygments in Phase A) — variable references and definitions are visually distinct, decorators get their own slot, type annotations are dimmed but readable, magic methods don't accidentally collide with builtins.
3. Code is displayed in the Axiom code theme (graphite background `#2e2e2e`, off-white tokens `#f4f1ec`, UT burnt-orange accent `#BF5700` for emphasis spans, brand-anchored to `spec-brand-identity.md`) with a 2-space gutter showing line numbers when block > 10 lines, language badge in the top-right corner of the block.
4. On first encounter with a code block, a one-time advisory line: `Tip: install JetBrains Mono / Fira Code / Cascadia Code for ligatures (=> != == >=). Hide with /hint suppress code-font.`
5. Code blocks emitted by the agent that *modify a file* go through the existing `diff_render` path so the user sees the change as a typed diff, not a re-paste.
6. `/share <code-block-ref>` produces a federation-resolvable short URL. Receipt includes language, formatter pass status, linter pass status, type-check pass status (when available for the language) — Phase B.

### Journey 3 — Pillar 3: auto-chart, theme, share (researcher cross-institution)

1. Researcher: "plot accuracy vs epoch for these three runs" with attached JSONL.
2. Data-shape sniffer: 3 series × time-axis numeric × shared x → grouped line plot.
3. Chat-context override hook: question contains "vs" and a quantitative axis named "epoch" → confirm line plot (deterministic; LLM tiebreak only if ambiguous, per ADR-039 D5).
4. Chart renders with the Axiom theme (graphite background `#2e2e2e`, off-white axes `#f4f1ec`, UT burnt-orange data accent `#BF5700`, per `spec-brand-identity.md` and `project_axiom_labs_brand`), high DPI, dual SVG + PNG output.
5. Inline preview in image-protocol-capable terminal; "saved to `~/.axi/scidisplay/figures/<hash>.svg`" line for plain terminals.
6. `/share` command: federation-hosted backend signs the figure with the producer's keypair, registers a short URL (`axiom://figure/<short-id>`), prints `https://<peer>/sci/<short-id>` for the colleague.
7. Colleague at peer institution clicks the URL: peer's Axiom resolves the signed artifact, verifies the producer's signature against the trust graph, displays figure + embedded reproduction recipe (data source pointer + Mermaid lineage + the exact code that produced it).

## 6) Success Metrics (8–12, measurable)

| # | Metric | Target | Measurement |
|---|---|---|---|
| M1 | Math render latency (typical equation, image-protocol terminal) | ≤ 50 ms p50, ≤ 150 ms p95 | Telemetry on first paint after submit |
| M2 | Math render fidelity in plain VT100 | 100% of equations in the eval set produce valid Unicode that round-trips through `sympy.printing.pretty` | Snapshot test against curated set of 500 equations |
| M3 | Hallucinated computation rate | 0 — every numerical result must come from SymPy/NumPy/SciPy/mpmath; the LLM never proposes a numeric answer | Static check: every `result_block` carries a SymPy provenance hash; CI fails if any result_block lacks one |
| M4 | Computation correctness on canonical eval set | ≥ 99% on the SymPy benchmark subset we curate (200 expressions) | Nightly CI eval |
| M5 | Long-job federation routing success | ≥ 95% of jobs flagged `>30s expected` route to a peer if a willing capable peer exists | Post-hoc telemetry sampled per session |
| M6 | Auto-chart selection accuracy | ≥ 90% on a curated test set of 100 (data-shape, intent) → expected-chart-type pairs | Eval suite gated by `axi ext eval` |
| M7 | Chart render p95 latency (typical 3-series, ≤ 10k points) | ≤ 800 ms end-to-end (sniff + render + display) | Telemetry |
| M8 | `/share` round-trip latency (producer to peer-displayed) | ≤ 2 s p95 on a same-region federation; ≤ 10 s p95 cross-continent | Federation eval scenario |
| M9 | Provenance completeness | 100% of artifacts (equation, computation, chart) carry `(T, U, A, R)` tuple + signed reproduction recipe | Composition-service invariant test |
| M10 | Capability degradation gracefully | Plain VT100 terminal renders zero "broken ASCII" — every degraded path is a curated Unicode or "saved to <path>" fallback, never a `≈` improvisation | Snapshot tests across iTerm2, Kitty, WezTerm, Ghostty, vanilla xterm, tmux-over-ssh |
| M11 | Domain-pack policy adoption | At least 1 domain extension overrides the auto-chart policy via the documented hook within 60 days of GA | Extension registry survey |
| M12 | Time-to-first-equation for new user | ≤ 3 minutes from `axi chat` start to first rendered equation, no setup beyond extension install | First-run telemetry (opt-in) |
| M13 | Code-block render fidelity | 100% of code blocks across the top 20 languages (Python, JS, TS, Go, Rust, C, C++, Java, Kotlin, Swift, Ruby, PHP, SQL, Shell, HTML, CSS, YAML, JSON, TOML, Markdown) render with no theme-color collision and correct keyword/identifier/string boundaries | Snapshot tests across the 20-language fixture set |
| M14 | Code-block share-receipt completeness | 100% of `/share`-d code blocks carry language tag + formatter pass + linter pass + (when applicable) type-check pass attestations | Composition-service invariant test (Phase B) |

## 7) Scope — Phased to the Prague Runway

**Honest framing:** Prague class start is early June 2026 (≈ 5 weeks out as of 2026-05-01). The classroom + Keplo + federation seeding work is the main blocker. Scientific Displays is **not** Prague-critical, but a small Pillar-1 subset can ship in time to demo and to start providing value to instructor authoring. Pillars 2 and 3 are post-Prague.

### Phase A — Prague-eligible closed loop (target: pre-rehearsal buffer 2026-05-22; *stretch*)

Phase A delivers the **whole asymmetric loop** at small scale — render, input, deterministic compute, browser-fallback — not just rendering. Without the compute kernel, "math rendering" is indistinguishable from any LaTeX renderer; the asymmetric edge IS the closed loop, so the loop ships in Phase A even at the cost of pushing Phase A past pure rendering scope.

- A1. Math rendering core: LaTeX → matplotlib mathtext SVG → image-protocol display in iTerm2 + Kitty + WezTerm + Ghostty.
- A2. Plain-terminal fallback via `sympy.printing.pretty`.
- A3. Quick-code expansion table (`\alpha → α` etc.) — deterministic substitution before render.
- A4. Inline chat surface integration — equations rendered inside chat the same way Mermaid blocks are today (mirror the `_MMDC_PATH` / `_process_mermaid_blocks` pattern in `chat/fullscreen.py`).
- A5. Provenance receipt stub (no signing yet — receipts are local-only IDs in Phase A; full signed `(T, U, A, R)` arrives in Phase B).
- A6. **Deterministic computation kernel in sandboxed subprocess (SymPy + NumPy + SciPy + mpmath).** Without it, math rendering is just rendering — the asymmetric edge is "LLM proposes, kernel verifies." (Was B1.)
- A7. **`/math` slash command — multi-line editor with live preview before submission.** The primary user input modality for non-trivial expressions; without it the input pipeline forces single-line LaTeX through the regular chat input. (Was B2.)
- A8. **MathJax browser fallback.** Web sessions need parity with terminal rendering on day one — the audience that will validate the harness for science work includes browser-only users (instructors prepping materials, peer reviewers). (Was B6.)
- A9. **Code-block syntax highlighting** via Pygments through Rich's `Syntax` (true-color terminals; ANSI-256 fallback). Per-language lexer auto-detection; explicit lexer hint via fence info-string (` ```python `) or path attribute. Line-number gutter when block > 10 lines.
- A10. **Axiom code theme** — three brand-anchored themes (`axiom-dark` default, `axiom-light`, `axiom-high-contrast`) palette-derived from `spec-brand-identity.md` (graphite `#2e2e2e` / off-white `#f4f1ec` / UT burnt-orange `#BF5700` accent). Themes published as Pygments style classes for portability into `bat`, GitHub gists, etc.
- A11. **Ligature-font advisory** on first code-block render per session: one-line tip recommending JetBrains Mono / Fira Code / Cascadia Code; suppressible via `/hint suppress code-font`. No font-shipping; this is an advisory, not an enforcement.
- A12. **Language-badge + diff-aware integration** — every rendered code block shows a corner badge (language + line count); blocks emitted as part of a `write_file` action route through the existing `diff_render.py` path (already shipped) so changes appear as typed diffs, not re-pastes.

**Phase A non-goals:** federation routing of compute, chart generation, sharing, tree-sitter semantic highlighting (lands in B), code-share receipts (B), LSP-backed inline diagnostics (post-D).

### Phase B — Provenance + backgrounding + alternate input + semantic code (post-Prague; target: 2026-Q3)

- B1. AsciiMath alternative input. (Was B3.)
- B2. Provenance receipts: full `(T, U, A, R)` tuple, signed by the producing node's key. (Was B4.)
- B3. Background-job protocol integration with `axiom.agents.background_service` Coordinator (status, progress, cancel, re-display on completion). (Was B5.)
- B4. **Tree-sitter semantic highlighting** for the top 20 languages. Pygments stays as the fallback when no tree-sitter grammar is installed. The semantic upgrade is what closes the "better than every other harness" gap — variable-vs-definition distinction, scope-aware coloring, type-annotation dimming, decorator slot. Patterned after Helix editor's tree-sitter integration; grammars vendored.
- B5. **Code-share receipts.** When a code block is `/share`-d, the receipt is composed from: language version, formatter pass status (black/ruff/prettier/gofmt/etc per language), linter pass status (ruff/eslint/clippy/etc), and where applicable type-check pass status (pyright/tsc/mypy). Receipts are signed by the producing node's keypair and resolve through the federation directory the same way figure shares do.

### Phase C — Federation + auto-charts (target: 2026-Q4)

- C1. Federation routing of long-running jobs per `spec-model-routing.md` + ADR-037 federation directory.
- C2. Pre-emptive cost estimator (cheap deterministic heuristic on input shape).
- C3. Auto-chart selector with deterministic rule table + LLM tiebreak hook.
- C4. matplotlib Axiom theme (graphite + off-white + UT burnt-orange `#BF5700`); SVG + PNG dual output.
- C5. Per-domain auto-chart policy override file format.
- C6. Sharing protocol: pluggable backends (filesystem, S3, SeaweedFS, federation-hosted).
- C7. Federation-hosted sharing: signed artifacts, peer resolution, embedded reproduction recipe (data pointer + Mermaid lineage + code).

### Phase D — Polish + ecosystem (target: 2027-Q1)

- D1. WebGL/Plotly fallback for browser when interactive charts requested.
- D2. Notebook export (`.ipynb`) preserving full provenance.
- D3. Domain-pack starter set (3 reference domain policies).
- D4. AEOS Gold conformance (behavioral attestation, quarantine recoverable).

## 8) Non-Functional / Constraints

- **Domain-agnostic.** Per `feedback_axiom_domain_agnostic`: no domain-specific or course-name references in any module, doc, or default. The capability serves any deep-science domain.
- **AEOS-conformant** at Bronze from Phase A; Silver from Phase B; Gold from Phase C.
- **Deterministic trust boundary** (AEOS §3.4): the LLM proposes; deterministic code authorizes and executes. The computation kernel never accepts model-generated code paths without first round-tripping through SymPy parse → AST → deterministic executor.
- **Sandbox.** Computation runs in a subprocess with no inherited file handles, no network, a memory cap, and a wall-clock cap. (Container/eBPF tighter sandboxes are an open question — see ADR-039 D3.)
- **Federation-native.** Every shareable artifact carries publisher signature + cohort scope; resolution honors the trust graph (ADR-028) and validated classification (ADR-027 + spec-classification-boundary).
- **No backward-compat shims.** Greenfield extension; no legacy surface to preserve. (Per `feedback_no_backward_compat_shims`.)
- **Stays out of:** `src/axiom/extensions/builtins/classroom/`, `src/axiom/rag/`, `src/axiom/extensions/builtins/rag*/` — Keplo session is shipping in parallel.
- **Mirrors the Mermaid pattern.** `chat/fullscreen.py` already has a clean precedent (`_MMDC_PATH`, `_render_mermaid_svg`, `_process_mermaid_blocks`, in-cache deduplication, fallback when binary missing). Math + chart rendering re-use that shape rather than inventing a new one.

## 9) Risks & Mitigations

| # | Risk | Mitigation |
|---|---|---|
| R1 | Terminal image-protocol fragmentation (iTerm2 vs Kitty vs Sixel vs nothing) ships a confusing UX. | Capability detection at chat-session start; explicit per-terminal eval matrix per Phase A; fall back to "saved to <path>" with the path printed clickably. |
| R2 | SymPy parse errors on user-typed LaTeX produce ugly errors. | Two-pass: try `sympy.parsing.latex.parse_latex`; on failure, render the LaTeX as a figure (still useful) and surface a structured "couldn't symbolically evaluate" line — the equation is still typeset, only the computation is missing. |
| R3 | Federation routing of compute leaks data inappropriately. | Route only to peers in the same cohort + at-or-above the data's classification ceiling; reuse spec-classification-boundary primitives, do not invent a new policy layer. |
| R4 | Auto-chart selector is wrong in a way that's hard to discover. | Always print the deterministic decision rule that fired ("rule: 3 numeric series + shared x-axis → grouped_line") so the user can argue with it; per-domain override file is documented from day one. |
| R5 | Phase A scope creep into Prague timeline. | Pillar-1-only for Phase A; explicit non-goals listed (no computation, no charts, no sharing); reviewer sign-off (Ben) required before any expansion. |
| R6 | matplotlib's render quality is good but not Mathematica. | The tradeoff is explicit — matplotlib mathtext + STIX font is the chosen quality bar (great, not perfect); KaTeX/MathJax for the browser surface is a higher bar but only available there. Document the tradeoff so users know what they're getting. |
| R7 | Sandbox escapes via SymPy's `eval` paths. | Use `sympy.parsing.sympy_parser.parse_expr` with `evaluate=False` and a restricted local namespace; never `sympify` raw strings; subprocess isolation is the second line of defense. |
| R8 | Sharing creates a long-lived public surface that's a security liability. | Federation-hosted sharing defaults to cohort-scoped access; cross-cohort requires explicit `--public` flag; revocation supported via the same gossip primitive ADR-037 uses. |

## 10) Acceptance & Rollout

**Sign-off:** Ben Booth (product + eng).

**Rollout plan:**

- **Phase A:** ships in `axi` 0.10.x as an opt-in extension (`axi ext install scidisplay` resolving to a built-in). Default off in chat until M1 (latency target) verified locally.
- **Phase B:** default on for `axi chat` after passing M1, M2, M3, M9 in CI for two consecutive releases.
- **Phase C:** federation routing default off; opt-in per cohort; verified across the Prague rehearsal cohort + at least one INL cohort before broad enable.
- **Phase D:** GA when M1–M12 all green for one full release cycle.

**Rollback criteria:** any of M3 (hallucinated results), M9 (provenance completeness), or R3 (federation data leak) fail in production telemetry → immediate disable in next patch release; defer the failing pillar until the gating metric is restored.

## 11) Contacts & Links

- Product / Eng lead: Ben Booth (no-reply@axiom-os.ai)
- Spec: `docs/specs/spec-scientific-displays.md`
- ADR: `docs/adrs/adr-039-scientific-displays.md`
- Brand reference: `docs/specs/spec-brand-identity.md`, `project_axiom_labs_brand`
- Adjacent capabilities: federation directory (ADR-037), model routing (`spec-model-routing.md`), background service (`axiom.agents.background_service`), Mermaid render precedent (`chat/fullscreen.py` lines 615–730).

---

_Copyright (c) 2026 B-Tree Ventures, LLC. Apache-2.0 licensed._
