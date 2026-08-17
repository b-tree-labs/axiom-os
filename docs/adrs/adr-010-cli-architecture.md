# ADR-010: CLI Architecture — Neut as Agentic Terminal

**Status:** Accepted
**Date:** 2026-03-16
**Decision Makers:** Ben Booth

## Context

The `axi` CLI is the primary interface to Axiom. It serves three audiences
simultaneously:

1. **Humans** — running commands in a terminal (`axiom signal brief`)
2. **The chat agent** — choosing CLI commands based on user prompts
3. **External agents** — invoking operations via MCP or subprocess

The CLI must feel natural to all three. For humans, it should work like a
well-designed Unix tool. For the chat agent, it should be the preferred
action surface — deterministic, testable commands over ad-hoc LLM code
generation. For external agents, it should expose discoverable operations
with structured output.

### Design Inspiration: Claude Code

Claude Code demonstrates that a CLI can be an excellent agentic interface:
- Terminal-native (no browser required)
- Observes terminal context (file changes, command output)
- Slash commands for common operations (`/clear`, `/help`, `/model`)
- Full tool use for complex operations
- Streaming output with rich formatting

Neut extends this pattern with **domain-specific CLI nouns** (signal, pub,
connect, tidy) and **deterministic command bias** — preferring known CLI
operations over generated code when a match exists.

## Decision

### 1. Noun-Verb CLI Pattern

```
axiom <noun> <verb> [args] [--flags]
```

Each noun is registered by an extension via `axiom-extension.toml`. The CLI
registry discovers extensions at startup and builds the command tree.

**Current nouns (v0.4.0):**

| Noun | Extension | Kind | Description |
|------|-----------|------|-------------|
| `signal` | signal_agent | agent | Signal ingestion pipeline |
| `chat` | chat_agent | agent | Interactive LLM assistant |
| `tidy` | mo_agent | agent | Resource steward |
| `doctor` | doctor_agent | agent | AI diagnostics |
| `mirror` | mirror_agent | agent | Public mirror sensitivity gate |
| `pub` | publisher | tool | Document lifecycle |
| `rag` | rag | tool | Knowledge retrieval |
| `db` | db | tool | Database management |
| `demo` | demo | tool | Guided walkthroughs |
| `ext` | — | builtin | Extension management |
| `config` | — | builtin | Onboarding wizard |
| `settings` | settings | utility | User preferences |
| `status` | status | utility | System health |
| `update` | update | utility | Self-update |
| `note` | note | utility | Quick notes |
| `connect` | — | planned | Connection management |

### 2. Chat Agent CLI Bias

When the user types a natural language prompt in `axiom chat`, the agent should
check if a CLI command matches before generating ad-hoc responses:

```python
# Conceptual flow in ChatAgent.turn()
def turn(self, user_input: str) -> str:
    # 1. Check if input maps to a known CLI command
    cli_match = self._match_cli_command(user_input)
    if cli_match and cli_match.confidence > 0.8:
        # Run the deterministic command
        result = subprocess.run(cli_match.command, capture_output=True)
        return self._format_cli_result(result, cli_match)

    # 2. Otherwise, use LLM with tools
    return self._llm_turn(user_input)
```

**Why bias toward CLI?**
- **Deterministic:** `axiom signal brief` always does the same thing
- **Testable:** CLI commands have unit tests; LLM outputs don't
- **Auditable:** CLI invocations are logged in the routing audit trail
- **Efficient:** No LLM round-trip for known operations
- **Explainable:** User can see exactly what command was run

**When to use LLM instead:**
- No CLI command matches the intent
- User asks a question (not an action)
- User explicitly wants LLM reasoning (`/ask ...`)
- Complex multi-step operations that need planning

### 3. Slash Commands (Human Shortcuts)

Slash commands are human-centric shortcuts available inside `axiom chat`:

| Command | Maps To | Description |
|---------|---------|-------------|
| `/brief` | `axiom signal brief` | Catch up on signals |
| `/status` | `axiom status` | System health |
| `/publish <file>` | `axiom pub push <file>` | Publish a document |
| `/connect` | `axiom connect` | Manage connections |
| `/update` | `axiom update` | Self-update |
| `/model <name>` | Change LLM model | Switch model mid-session |
| `/mode <tier>` | Change routing mode | Switch public/EC routing |
| `/help` | Show available commands | — |
| `/clear` | Clear chat history | — |
| `/complete` | End review session | — |

Slash commands are a strict subset of what the full CLI provides. They
exist for quick access during chat, not as a separate command surface.

### 4. Terminal Activity Monitoring

Neut can observe terminal context and offer to continue in chat:

```
$ axiom signal status
  Inbox: 3 unprocessed signals
  Drafts: 0

$ axiom chat
  [Neut noticed: you have 3 unprocessed signals]

> process them
  Running: axiom signal ingest
  ✓ 3 signals processed
  Running: axiom signal draft
  ✓ Weekly summary drafted at runtime/drafts/...
```

**Implementation:** When `axiom chat` launches, it checks:
1. Recent CLI command history (shell history file or `.axi/last_command.json`)
2. Current working directory state (any pending signals, drafts, etc.)
3. Session context (resumed sessions carry prior CLI context)

This is **opt-in** and **read-only** — Neut observes but doesn't modify
terminal state without explicit user action.

### 5. Structured Output (--json)

All CLI commands support `--json` for machine consumption:

```bash
axiom status --json          # JSON health report
axiom signal status --json   # JSON signal counts
axiom connect --json         # JSON connection list
```

This enables:
- External agents to parse output reliably
- CI/CD pipeline integration
- Dashboard data collection
- Scripting and automation

### 6. Technology Choice

**Current:** Python (argparse + argcomplete)

**Rationale:**
- Same language as all extensions (no FFI, no build step)
- Extensions register CLI commands via TOML + Python modules
- argcomplete provides shell tab completion
- Rich library provides terminal formatting
- Prompt Toolkit provides the chat TUI

**Future consideration:** Node.js (TypeScript)

Claude Code uses Node.js for its CLI. If Axiom needs:
- Faster startup time (Python's ~200ms import overhead)
- Better terminal rendering (ink/React for TUI)
- Easier distribution (single binary via pkg/nexe)

...then a Node.js rewrite of the CLI shell (keeping Python extensions via
subprocess or IPC) would be worth evaluating. This is an optimization, not
a current bottleneck.

**Decision:** Stay Python until startup latency or distribution becomes a
user-reported problem. The extension system and all domain logic remain
Python regardless of CLI shell technology.

## Consequences

### Positive
- CLI is the single source of truth for all operations
- Chat agent uses tested, deterministic commands
- Slash commands give humans fast access
- Terminal monitoring creates fluid CLI ↔ chat transitions
- `--json` makes CLI machine-readable for external agents
- Python keeps everything in one language

### Negative
- CLI bias requires maintaining prompt → command mapping
- Terminal monitoring adds complexity
- Python startup is slower than compiled alternatives
- Two rendering paths (REPL + TUI) to maintain

### Mitigations
- Prompt → command mapping uses extension registry (auto-discovered)
- Terminal monitoring is opt-in, degrades gracefully
- Startup time mitigated by lazy imports (only load what's needed)
- TUI is optional; REPL is the fallback

## Related Documents

- [ADR-006: MCP Server + Agentic Access](adr-006-mcp-agentic-access.md)
- [axiom CLI Spec](../tech-specs/spec-axi-cli.md) — Command structure and extension registration
- [Connections Spec](../tech-specs/spec-connections.md) — `axiom connect` command
- [Model Routing Spec](../tech-specs/spec-model-routing.md) — Chat agent routing
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
