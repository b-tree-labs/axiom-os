# graduation

Shadow-mode outcome logging for internal classifiers — evidence accumulates
before any rule graduates.

Phase 1 shadows ONE call site: the LLM tier/routing classification
(`axiom.llm.router.QueryRouter.classify`). Every routing decision's *shape*
is appended to a local outcome log, so a 30-day evidence base builds up for
an eventual, evidence-gated graduation of the deterministic rule.

## Guarantees (phase 1)

- **Shadow only.** The live routing result is never altered. Observation
  runs in the router's best-effort zone (same posture as the audit write)
  and can never raise, block, or slow a classification.
- **Nothing leaves the machine.** The SDK backend is constructed with an
  explicit `NullEmitter` and local file storage; the fallback backend is
  plain local JSONL via the platform's `locked_append_jsonl`.
- **Decisions, never data.** Records carry decision shapes only — tier,
  classifier stage, basis, sensitivity, booleans, counts. Never message
  text, never the matched keyword terms, never free-text reason strings.

## Recorded shape

One `ClassificationRecord`-shaped JSONL row per decision, with
`input` = the content-free feature dict:

```
phase, session_mode, sensitivity, context_turns,
classifier, basis, keyword_matched, slm_tier, failure_reason
```

plus `label` (the tier the live path chose), `rule_output` (the mirror
rule's prediction), `outcome` (`correct` when mirror and live path agree),
`source`, `confidence`, `timestamp`.

Log location: `<user state dir>/graduation/outcomes/llm_tier_routing/outcomes.jsonl`
(`~/.axi/...` by default; `AXI_STATE_DIR` honored).

## Backends

- `pip install 'axiom-os-lm[graduation]'` installs the optional `postrule`
  SDK: the mirror rule is wrapped with `@ml_switch` pinned to `Phase.RULE`
  (`phase_limit=RULE` — the switch cannot advance), and verdicts flow
  through the SDK's rotating, flock-guarded `FileStorage`.
- Without the SDK the extension degrades to a thin recorder writing the
  identical row shape to the same path with `locked_append_jsonl`. The log
  is continuous across either backend.

## Enablement

On by default in real processes. Overrides, in precedence order:

1. `AXIOM_GRADUATION_SHADOW=0|1` — explicit per-process override.
2. Under pytest (`PYTEST_CURRENT_TEST` set) recording is OFF unless forced
   by (1), so test runs never taint the dogfood outcome log.
3. `graduation.shadow` setting (`axi settings`), default `true`.

Enablement is evaluated once per process (at the router's lazy observer
bootstrap); changing the setting takes effect on the next process start.

## CLI

- `axi graduation status [--json]` — switch, phase, backend, record
  counts, time range, log path. Thin wrapper over the `graduation.status`
  skill function (ADR-056).

## Explicitly OUT of scope (phase 1)

- **Any promotion/graduation logic.** No phase advancement, no gates, no
  challenger models — `phase_limit=RULE` enforces this at the SDK level.
- **The export-control classifier itself.** The keyword/SLM pipeline is
  observed only through the decision shapes it already emits; nothing in
  its logic is touched, replaced, or reimplemented.
- **LLM-judge verifiers.** No verifier is configured; outcomes are the
  mirror-agreement approximation only.
- **Pricing / hosted telemetry.** No hosted service is ever contacted.
