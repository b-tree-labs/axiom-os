# Axiom Chat Model-Picker Spec

> 🔲 **SPEC'D** — 2026-05-22. Targets `axi chat` / `neut chat`. Wires
> together [[spec-model-routing]] §13 (ModelStrategy) + §14 (`auto`
> user mode) + [[spec-federation]] §6.6 (install-time onboarding) on
> the chat-loop UX side. Implementation TBD; the slash command
> `/model` already exists as a session-level switch — this spec
> documents its current behavior and the per-prompt + visibility
> polish that brings UX to Cursor parity.

---

## 1. Problem Statement

Once a user has multiple LLM providers configured (typical after
the install-time federation onboarding adopts a self-hosted endpoint
alongside a cloud key, or after the user adds Anthropic + OpenAI
to their config), they need three things the chat loop doesn't
currently surface cleanly:

1. **Knowing which model is about to answer.** When the default
   is `auto`, the user has lost predictability. A status line
   showing the next-turn provider is the minimum.
2. **Switching the default for a session.** Already exists as
   `/model <name>` (`axiom/extensions/builtins/chat/commands.py`
   `cmd_model_switch`). Documented here for completeness; behavior
   unchanged.
3. **Overriding the default for a single prompt** without
   switching the session default. Cursor calls this an
   "inline model swap." The `/model` slash command can't do this
   because it changes session state, not per-turn state.

Cursor's UX is the reference target: status indicator near the
input, per-prompt override via a prefix (or picker dropdown), and
the resolved-model attribution stamped on each response.

---

## 2. Design

### 2.1 Three orthogonal selection layers

```
                 effective provider for this turn
                              ▲
                              │
              ┌───────────────┼───────────────┐
              │               │               │
       per-prompt        session default    config default
       override          (`/model X`)       (`gateway.default_routing`
       (`@X prompt`)                          + ModelStrategy)
              │               │               │
            highest        next           lowest precedence
            precedence     precedence
```

A per-prompt override (§3) wins for that turn only. A session
override (§4 `/model`) is sticky until cleared or chat exits.
Otherwise the config default (`pinned` provider or `auto` →
ModelStrategy per spec-model-routing §14) applies.

### 2.2 What this spec does NOT cover

- **Provider registration / credentials** — that's
  [[spec-connections]] §3 + the install-time onboarding in
  [[spec-federation]] §6.6.
- **The routing decision itself** — that's [[spec-model-routing]]
  §13 ModelStrategy + §14 `auto` mode.
- **Per-turn cost accounting** — exists in the gateway; this spec
  only requires it to be displayed.

---

## 3. Per-Prompt Override Syntax

Two inline syntaxes; both must be supported. They are mutually
exclusive per turn.

### 3.1 `@provider-name` prefix

```
> @anthropic explain the difference between PWR and BWR primary loops
```

Rules:

- The `@` must be the first non-whitespace character of the line.
- The provider name matches `[a-zA-Z0-9_-]+` greedily; it ends at
  the first whitespace.
- Provider name is matched against the configured
  `[[gateway.providers]]` `name` field (case-insensitive).
- Unknown names produce an inline error before the turn fires:
  `Unknown provider 'foo'. Known: anthropic, private-llm, openai.
  Use /model to list with status.`
- The `@<name>` prefix is stripped before the prompt is sent to
  the LLM. The audit log retains both the original input and the
  stripped prompt.

### 3.2 `/m` short slash

```
> /m private-llm explain the difference between PWR and BWR
```

Equivalent to `@private-llm explain…`. Provided because `@` may
clash with mention syntax in chat tools that some users layer on
top (Markdown @mentions, etc.); `/m` is unambiguous.

### 3.3 Tab completion

In both syntaxes, tab after the `@` or after `/m ` cycles through
configured provider names. The completion source is the live
gateway provider list, so providers added via federation onboarding
or `axi connect` appear without a chat restart.

---

## 4. Session Default Override

`/model` already exists. Behavior documented for completeness:

| Invocation | Effect |
|---|---|
| `/model` | List configured providers with `✓ active`, `⚠ no key`, `✗ unreachable` indicators. No state change. |
| `/model <name>` | Set the session default to `<name>`. Subsequent turns route to `<name>` unless per-prompt overridden (§3). |
| `/model auto` | Set the session default to `auto`; subsequent turns resolve via ModelStrategy (spec-model-routing §14). |
| `/model reset` | Clear the session override; revert to `gateway.default_routing` from config. |

Session state is not persisted across chat sessions; each new
`axi chat` starts from the config default.

### 4.1 The product's default model

A product can name the model its people should chat with through
`BrandingConfig.chat_provider_fn`, which returns a provider spec
(`name`, `endpoint`, `model`, `api_key_vault`, plus any optional
`LLMProvider` field). Chat offers it ahead of the configured providers,
which makes it the session's starting model. Typically this is the chat a
deployment serves over its own data.

| Situation | Starting model |
|---|---|
| `--local` | The machine's configured providers; the product default is not offered. |
| `--provider <name>` or `routing.prefer_provider` set | The person's choice, as before. |
| The vault holds no entry named `api_key_vault` | The machine's configured providers. The product's start lines say how to get a key. |
| Otherwise | The product's model. The start line names it and the host serving it. |

The hook must not raise or touch the network; it runs before every session.

### 4.2 Node data tools on a machine with no node

The `data` namespace reads this node's own database. When
`chat.tool_namespaces` names it and the platform database
(`AXIOM_DB_URL`) does not answer on this machine, those tools are not
offered, because from here they can only answer "no data". The
`chat.local_data_tools` setting overrides this: `auto` (the default),
`always`, or `never`.

---

## 5. Status-Line Indicator

Above the input line, a one-line status shows the *next-turn*
resolved provider:

```
↳ anthropic/sonnet · auto (cost-conservative) · cost-est ~$0.003/turn
> _
```

Fields:

| Field | Source |
|---|---|
| Resolved provider + model | `Gateway` peek at `ModelStrategy.resolve(role=EXECUTOR, ctx=session_ctx)` |
| Mode | `pinned` / `auto` / `<provider-name>` / `session: <name>` |
| Cost estimate | Per-token rate × an empirical average turn length, or "free" if `cost_per_token_usd == 0` |

When `auto` is in effect, the resolved provider is the *prediction*
based on an empty prompt; the *actual* provider for a given turn
is footer-stamped on the assistant's response (§6).

When the session has a per-`/model` override, the status line
shows `session: <name>` and the mode line is the override, not
the config default.

Status line is suppressed when the chat session is non-TTY (piped
input, agent-orchestrated calls) — it would just be noise in
those contexts.

---

## 6. Resolved-Provider Footer Stamp

Each assistant response is stamped with a single dim-grey line
showing the actual provider + role + cost:

```
↳ anthropic/sonnet · EXECUTOR · $0.003 · 412ms
```

Fields:

| Field | Source |
|---|---|
| Provider + model | `ResolvedAssembly.by_role[EXECUTOR]` |
| Role | The role consulted for this turn (default `EXECUTOR`) |
| Cost | Actual cost from the response metadata (or "free" if zero) |
| Latency | Server-side time-to-first-byte; useful in federation contexts where a peer may be slow |

In `auto` mode, the footer also surfaces a brief rationale when
the resolved provider differs from what the status line predicted:

```
↳ anthropic/sonnet · EXECUTOR · $0.003 · 412ms
  (private-llm was preferred but health-degraded; fell over)
```

---

## 7. Keyboard Shortcut

`Ctrl+.` (matching Cursor's binding) opens an inline picker
overlay populated from the configured provider list:

```
┌─ Pick a model ───────────────────────────────┐
│ ▶ anthropic        Anthropic Claude Sonnet 4 │
│   private-llm-rag  Self-hosted Qwen + RAG    │
│   openai           OpenAI GPT-4o             │
│   auto             ModelStrategy (default)    │
└──────────────────────────────────────────────┘
  ↑/↓ to navigate, Enter to use for next prompt,
  Shift+Enter to set as session default
```

The picker is purely additive UX on top of §3/§4 — it produces
exactly the same per-prompt-override or `/model` effect that the
syntaxes do. Users on terminals where the keyboard binding is
unavailable (constrained TTYs, screen-reader users) get the same
result through §3 and §4.

---

## 8. Audit + observability

Every chat turn's audit record (existing chat-session log)
captures:

| Field | Why |
|---|---|
| `requested_provider` | What the per-prompt or session override asked for, or `auto` if none |
| `resolved_assembly` | The `ResolvedAssembly` (spec-model-routing §13.2) — full per-role choice |
| `selection_rationale` | Why this assembly won (e.g., "preferred provider private-llm unreachable; fell to anthropic") |
| `effective_cost_usd` | What the user was actually charged (or 0 for free providers) |

Existing chat session storage is the substrate; no new schema
required beyond the four fields above.

---

## 9. Implementation Notes (non-normative)

- The status line (`§5`) is rendered by the chat REPL between
  turns. It needs a no-op fast path that handles "no providers
  configured" (show `↳ no LLM configured · run /config`).
- The picker overlay (`§7`) reuses the chat REPL's existing
  full-screen mode if available; falls back to a one-shot inline
  prompt on plain readline-style terminals.
- Tab completion (`§3.3`) hooks the existing input-provider
  completion plumbing (the same path that completes `/` slash
  command names today).
- The `Ctrl+.` binding (`§7`) should not be claimed when the
  user's terminal already binds it to something else; conflict
  detection at chat startup.

---

## 10. Related Documents

- [[spec-model-routing]] §13 ModelStrategy — the resolution
  layer this UX feeds and consumes.
- [[spec-model-routing]] §14 `auto` user mode — the config-layer
  prerequisite for §5 status-line "auto (cost-conservative)".
- [[spec-federation]] §6.6 Install-Time Onboarding UX — populates
  the multi-provider state that makes per-prompt switching
  worthwhile.
- [[spec-connections]] §2.4 `axiom connect` — the credentials-side
  flow; orthogonal but adjacent.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
