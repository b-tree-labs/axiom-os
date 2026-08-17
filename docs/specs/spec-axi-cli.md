# Axiom CLI Technical Specification

**`axi` — the command-line interface for the Axiom intelligence platform**

---

| Property | Value |
|----------|-------|
| Version | 0.6.2 |
| Last Updated | 2026-05-02 |
| Status | Active Development |
| Package | [axi-platform](https://pypi.org/project/axi-platform/) on PyPI |
| PRD | [CLI PRD](../prds/prd-axi-cli.md) (see [§Status & Phasing](../prds/prd-axi-cli.md#status--phasing) for what's shipped vs designed) |
| Sub-spec | [Extension Loading](spec-extension-loading.md) |
| Sub-PRD | [Cross-harness Commands Generator](../prds/prd-commands-generator.md) |
| Brand | [CLI Identity](spec-brand-identity.md#cli-identity) |

---

## Overview

`axi` is the command-line interface for Axiom. It uses a **noun-verb pattern** (`axi <noun> <verb> [args] [--flags]`) where each noun is registered by a builtin or user extension via `axiom-extension.toml` manifests.

### Architecture

| Component | Choice | Rationale |
|-----------|--------|-----------|
| **Language** | Python 3.11+ | Rapid iteration, rich ecosystem (Whisper, ML, openpyxl) |
| **CLI framework** | argparse + argcomplete | Standard library, shell completions |
| **HTTP client** | httpx | Async, TLS, connection pooling |
| **Output** | rich + json | Pretty tables/markdown, machine-readable JSON |
| **Config** | TOML (tomlkit) | Human-editable, standard format |
| **Package** | `axi-platform` on PyPI | `pip install axi-platform` |
| **Entry point** | `axi` / `axiom` | Both work; `axi` is the short form |

### Package Structure

```
axiom (Python package)
├── axiom_cli.py              # Entry point, dispatch, core commands
├── extensions/
│   ├── discovery.py          # Extension + CLI command discovery
│   └── builtins/             # All builtin extensions (each registers CLI nouns)
│       ├── chat/       # chat, code
│       ├── signals/        # signal
│       ├── publishing/        # pub, doc
│       ├── hygiene/         # tidy
│       ├── federation/       # federation, nodes, knowledge, research, security, chaos
│       └── ...               # 23+ extensions total
└── infra/                    # Shared infrastructure (gateway, router, etc.)
```

Commands are discovered dynamically from `axiom-extension.toml` manifests in each extension directory. Core commands (`config`, `setup`, `ext`, `infra`, `doctor`) are hardcoded in `axiom_cli.py` and take precedence over extensions.

---

## Command Hierarchy

Complete command tree as implemented. Each noun is registered by an extension unless marked **(core)**.

```
axi
│
├── chat [message]             # Interactive agent with tool calling (chat)
│   ├── --resume <id>          # Resume an existing session
│   ├── --context <file>       # Load additional context
│   ├── --no-stream            # Disable streaming output
│   ├── --model <name>         # Override LLM model
│   ├── --provider <name>      # Override LLM provider
│   ├── --mode <auto|public|export-controlled>  # Routing mode
│   ├── --render <rich|ansi>   # Force render provider
│   ├── --input <ptk|basic>    # Force input provider
│   └── --no-tui               # Disable full-screen TUI
│
├── code                       # Alias for chat (chat)
│
├── signal                     # Agentic signal ingestion pipeline (signals)
│   ├── brief [topic]          # Catch up on what happened
│   │   ├── --since TIME       #   Time range start
│   │   ├── --hours N          #   Hours lookback
│   │   ├── --ack              #   Acknowledge all
│   │   ├── --caught-up        #   Mark caught up
│   │   ├── --status           #   Show brief status
│   │   ├── --topics           #   List available topics
│   │   └── --index            #   Index brief to RAG
│   ├── media search <query>   # Search recordings
│   │   ├── --mode <auto|keyword|semantic|hybrid>
│   │   ├── --limit N          #   Result count
│   │   ├── --play             #   Auto-play first result
│   │   └── --discuss          #   Discuss first result
│   ├── media play <id>        # Play a recording
│   ├── media discuss <id>     # Discuss recording with agent
│   ├── media stats            # Media library statistics
│   ├── media index            # Rebuild media index
│   ├── media list             # List indexed recordings
│   ├── draft                  # Generate weekly changelog
│   ├── status                 # Pipeline health
│   ├── watch                  # Watch endpoints for changes
│   ├── pipeline ingest        # Process voice memos / sources
│   ├── pipeline review        # Guided correction review
│   ├── pipeline suggest       # LLM signal-to-PRD matching
│   ├── pipeline sources       # Manage signal sources
│   ├── pipeline subscribers   # Manage subscribers
│   ├── pipeline routes        # Signal routing
│   ├── pipeline providers     # Docflow provider status
│   ├── pipeline serve         # HTTP ingestion server
│   ├── pipeline voice         # Voice identification status
│   └── pipeline timestamps    # Regenerate word-level timestamps
│
├── pub                        # Document publishing lifecycle (publishing)
│   ├── overview               # Dashboard of document ecosystem
│   ├── publish [file]         # Generate + publish to storage
│   │   ├── --draft            #   Draft mode
│   │   ├── --all              #   All tracked documents
│   │   ├── --changed-only     #   Only changed files
│   │   ├── --endpoint EP      #   Target endpoint
│   │   └── --force            #   Skip confirmation
│   ├── review [file]          # Interactive human-in-the-loop review
│   │   ├── --fresh            #   Fresh review
│   │   ├── --quick            #   Quick scan
│   │   ├── --status           #   Show review status
│   │   └── --chat             #   Discuss findings
│   ├── generate <file>        # Generate locally (no upload)
│   ├── pull [doc_id]          # Pull external doc → update .md
│   │   ├── --all              #   All tracked
│   │   ├── --dry-run          #   Preview only
│   │   └── --comments         #   Include comments
│   ├── push [path]            # Push to storage (auto-assembles)
│   │   ├── --all              #   All tracked
│   │   ├── --draft            #   Draft mode
│   │   ├── --endpoint EP      #   Target endpoint
│   │   └── --headed           #   Include header
│   ├── status [file]          # Document state + provenance
│   ├── check-links            # Verify cross-document links
│   ├── diff                   # Changed docs since last publish
│   ├── scan [folders...]      # Scan for tracked/untracked docs
│   ├── onboard <id> <file>    # Register document in manifest
│   ├── watch                  # Auto-publish on save
│   ├── providers              # List available providers
│   └── assemble <manifest>    # Assemble multi-section document
│
├── doc                        # Alias for pub (publishing)
│
├── rag                        # RAG index management (rag)
│   ├── index [path]           # Index documents into RAG store
│   ├── search <query>         # Search the RAG index
│   ├── status                 # Per-corpus index statistics
│   ├── load-community         # Load pre-built community corpus
│   ├── sync <corpus>          # Sync corpus from remote source
│   ├── watch                  # Watch workspace, re-index on change
│   └── reindex                # Force full re-index
│
├── federation                 # Federation membership (federation)
│   ├── status                 # Show federation status
│   ├── init                   # Initialize node identity
│   ├── join                   # Join a federation
│   ├── leave                  # Leave current federation
│   ├── invite                 # Generate invitation token
│   ├── resources              # List shared resources
│   └── peers                  # List federated peers
│
├── nodes                      # Fleet monitoring (federation)
│   ├── add                    # Register a node
│   ├── status                 # Check node health
│   ├── upgrade                # Run remote upgrade
│   ├── remove                 # Unregister a node
│   └── list                   # List all registered nodes
│
├── knowledge                  # Knowledge observatory (federation)
│   ├── status                 # Knowledge health dashboard
│   ├── velocity               # Ingestion rate
│   ├── accumulation           # What do we know?
│   ├── impact                 # Is knowledge being used?
│   ├── report                 # Full knowledge report
│   ├── gaps                   # Knowledge coverage gaps
│   └── saved                  # Saved findings from /save
│
├── research                   # Call to Research protocol (federation)
│   ├── create                 # Create a Call to Research
│   ├── list                   # List calls
│   ├── show                   # Show call details
│   ├── claim                  # Claim a research part
│   ├── submit                 # Submit response
│   ├── publish                # Publish synthesis
│   └── chain                  # Show research chain
│
├── security                   # SECUR-T guardian (federation)
│   ├── status                 # Security health
│   ├── alerts                 # List alerts
│   ├── resolve                # Resolve an alert
│   ├── trust                  # Show trust score
│   ├── rules                  # Anomaly detection rules
│   ├── escalation             # Escalation path health
│   └── scan                   # Run anomaly check
│
├── chaos                      # Chaos testing (federation)
│   ├── list                   # Available chaos scenarios
│   ├── run                    # Run a chaos scenario
│   └── status                 # Results from last run
│
├── connect [name]             # Connection management (connect)
│   ├── --clear                # Remove saved credentials
│   ├── --check                # Health check all connections
│   └── --json                 # Machine-readable output
│
├── agents                     # Agent service management (agents)
│   ├── status                 # Agent service status
│   ├── start [name]           # Start agent(s)
│   ├── stop [name]            # Stop agent(s)
│   ├── register               # Register as system services
│   ├── unregister [name]      # Remove registrations
│   └── logs [name]            # Tail agent logs
│
├── tidy                         # TIDY resource steward (hygiene)
│   ├── status                 # TIDY health
│   ├── ls                     # List tracked entries
│   ├── clean                  # Sweep expired/orphaned
│   ├── purge                  # Delete all scratch entries
│   ├── vitals                 # Detailed vitals snapshot
│   ├── diagnose               # LLM-powered diagnosis
│   ├── ci                     # CI/CD pipeline status
│   └── retention              # Data retention status
│
├── release                    # Release management (release + release)
│   ├── status                 # Build and release status
│   ├── mode                   # Developer vs operator mode
│   ├── patterns               # Known CI failure patterns
│   ├── check                  # Pre-push prevention checks
│   └── plan                   # Show release plan
│
├── log                        # Log inspection (log)
│   ├── tail                   # Stream recent routing events
│   ├── verify                 # Verify audit log chain integrity
│   ├── stats                  # Log statistics
│   ├── backend                # Backend configuration
│   ├── sinks                  # Configured sinks
│   └── export                 # Export logs
│
├── db                         # PostgreSQL infrastructure (db)
│   ├── up                     # Start K3D cluster with PG
│   ├── down                   # Stop cluster (preserve data)
│   ├── delete                 # Delete cluster and data
│   ├── status                 # Cluster and DB status
│   ├── migrate                # Alembic migrations
│   └── bootstrap              # Full setup from scratch
│
│                              # (legacy `mirror` gate retired 2026-04-24:
│                              #  content gate → publishing/PRESS;
│                              #  security scan → diagnostics/TRIAGE)
│   └── status                 # Mirror status
│
├── ide                        # IDE configuration (ide)
│   ├── status                 # Detected IDEs and extensions
│   ├── setup                  # Auto-configure IDEs
│   ├── extensions             # Install recommended extensions
│   └── syntax [lang]          # Install syntax highlighting
│
├── settings                   # Configuration (settings)
│   ├── get <key>              # Read a setting
│   ├── set <key> <value>      # Write a setting
│   ├── reset <key>            # Remove override
│   └── edit                   # Open in $EDITOR
│
├── note [text]                # Quick personal notes (note)
│   └── --list                 # Show recent daily note files
│
├── status                     # System health dashboard (status)
│   ├── --db                   # Database status
│   ├── --api                  # API status
│   ├── --services             # Service status
│   ├── --watch                # Continuous monitoring
│   └── --json                 # Machine-readable
│
├── test [profile]             # Test orchestration (test)
│   ├── quick|full|pr|release  # Test profiles
│   ├── unit|integration|...   # Specific suites
│   ├── --verbose              # Verbose output
│   ├── --fail-fast            # Stop on first failure
│   ├── --coverage             # Coverage report
│   └── --watch                # Re-run on change
│
├── update                     # Dependency updates (update)
│   ├── --deps                 # Update dependencies
│   ├── --migrate              # Run migrations
│   ├── --check                # Check for updates
│   └── --pull                 # Pull latest code
│
├── install                    # Environment setup (install)
│   ├── --env <name>           # Target environment
│   ├── --list                 # List available environments
│   ├── --force                # Force re-run
│   └── --step <id>            # Run specific step
│
├── serve                      # HTTP API server (http)
│   ├── --port PORT            # Listen port
│   ├── --host HOST            # Bind address
│   ├── --origins ORIGINS      # CORS origins
│   ├── --api-key KEY          # API key
│   ├── --read-only            # Read-only mode
│   └── --static-dir DIR       # Serve static files
│
├── config                     # (core) Interactive onboarding wizard
├── setup                      # (core) Alias for config
├── ext                        # (core) Extension management
├── infra                      # (core) Infrastructure detection
└── doctor | dr                # (core) AI-powered environment diagnostics
```

### Command Discovery

Extension commands are discovered at runtime from `axiom-extension.toml`
manifests. The AEOS-canonical form (per `spec-aeos-0.1.md`) declares each
verb as a `[[extension.provides]]` block with `kind = "cmd"`, optionally
annotated with capability tier and intent groups for the surfacing model
described below.

```toml
# axiom-extension.toml — AEOS-canonical form
[[extension.provides]]
kind = "cmd"
noun = "signal"
entry = "axiom.extensions.builtins.signals.cli:main"
description = "Agentic signal ingestion pipeline"
tier = "core"                          # starter | core | advanced | internal
intent_groups = ["maintain", "investigate"]
```

`tier` defaults to `core` if omitted; `intent_groups` defaults to `[]`.
Per-verb tier overrides may live alongside the noun-level declaration via
the `verb_overrides` map (e.g. `signal purge` is `advanced` even though
the noun's default tier is `core`).

Core commands (`config`, `setup`, `ext`, `infra`, `doctor`) are hardcoded
in `axiom_cli.py` and take precedence over any extension that registers
the same noun.

**Discovery order:**
1. Core commands (SUBCOMMANDS dict in `axiom_cli.py`)
2. Builtin extensions (`src/axiom/extensions/builtins/`)
3. Installed packages with `axiom-extension.toml` (PyPI extensions)
4. User extensions (`~/.axi/extensions/`)

---

## Capability Tiers and Familiarity Tracking

The PRD section *Progressive Disclosure* describes the user-facing model;
this section specifies the implementation contract.

### Effective surfacing rule

For any verb `V` provided by extension `E`, `V` is **surfaced** to the
current user when:

```
tier_rank(V) ≤ min(global_tier(user), familiarity_tier(user, E))
```

Where `tier_rank` orders tiers as `starter < core < advanced < internal`.
Surfacing controls all of:

- What `axi help` lists (without `--all`)
- What shell tab-completion suggests (without prior typing of the noun)
- What `axi commands generate` emits per harness (without `--tier`
  override)

`axi help --all`, `axi <noun> <Tab>` (after the noun is typed), and
`axi commands generate --tier all` always reveal the full surface — the
tier model gates *automatic* presentation, never *available* invocation.

### Per-extension familiarity

Familiarity is tracked in `~/.axi/competency.json`:

```json
{
  "global_tier": "core",
  "extensions": {
    "hygiene": {"familiarity_tier": "advanced", "first_used": "...", "verbs_used": {...}},
    "classroom": {"familiarity_tier": "core", "first_used": "...", "verbs_used": {...}}
  }
}
```

A new extension always starts at `familiarity_tier = "starter"` for
the user, regardless of `global_tier`. This is the "novice in foreign
territory" guarantee — installing an unfamiliar extension never floods
the user's surface with its full verb set.

### Reveal mechanism

Surfacing gates *automatic presentation*, never invocation. A user
may always type the full `axi <noun> <verb>` regardless of tier; the
reveal mechanism is for users who want to *see* the wider surface
without waiting for the graduation observer to catch up.

#### Reveal API

| Form | Effect | Persistence |
|---|---|---|
| `axi help <ext> --all` | Show all tiers for `<ext>` (excluding `internal`) | Session |
| `axi help <ext> --tier <name>` | Show up to and including `<name>` for `<ext>` | Session |
| `axi help <ext> --all --internal` | Include `internal` tier | Session |
| `axi <ext> <Tab><Tab>` | Widen completion for `<ext>` to all tiers (excluding `internal`) | Session |
| `axi help <ext> --all --pin` | Same as `--all`, also pinned to `~/.axi/competency.json` | Persistent |
| `axi competency set <ext> <tier>` | Direct write of per-extension familiarity | Persistent |
| `axi competency unset <ext>` | Remove pin; let the observer resume | Persistent |

#### State

Pinned reveals live alongside observed familiarity in
`~/.axi/competency.json`:

```json
{
  "global_tier": "core",
  "extensions": {
    "radprotect": {
      "familiarity_tier": "core",         // observed
      "pinned_tier": "advanced",          // user-set; takes precedence
      "first_used": "...",
      "verbs_used": {...}
    }
  }
}
```

When `pinned_tier` is set, the effective surfacing rule becomes:

```
emit(verb V from extension E) iff
  tier_rank(V) ≤ min(global_tier(user), pinned_tier(user, E) ?? familiarity_tier(user, E))
```

The observer continues tracking familiarity in the background but
does not adjust `pinned_tier`.

#### Discovery surfaces (where the reveal becomes findable)

| Surface | Trigger | Output |
|---|---|---|
| `axi help <ext>` footer | Hidden verb count materially exceeds shown count (heuristic: ≥ 5 hidden, or `internal` verbs present) | One dim line: `… N more in advanced/internal · 'axi help <ext> --all' to reveal` |
| `axi ext install` post-install | Always (one-time per install) | One line: `↑ See all verbs with 'axi help <ext> --all'` |
| Chat resolution preview | Resolved verb is at a tier above the user's effective surfacing | Reveal hint inside the existing P2 resolution-preview frame; user still confirms invocation normally |
| Tab-twice in shell | Second `<Tab>` within ~500ms after the first | Widen the per-session completion list for that extension |

#### Provenance

Every reveal action writes a CLI history entry with `via=reveal`
(see *CLI History and Decay* above). The graduation observer counts
reveal-followed-by-successful-invocation as an accelerated
familiarity signal — three reveals on `<ext>`'s `advanced` tier
followed by ≥ 3 successful invocations of `advanced` verbs there
graduates `<ext>` to `advanced` familiarity (subject to the standard
24h time-since-first-use floor).

This closes the loop: reveals teach the system that the user knows
what they're doing, so the reveal hints stop appearing once
observation catches up to the user's demonstrated competency.

### Hands-free graduation

Per-extension familiarity advances when **all** of the following clear:

| Signal | Threshold for advancement |
|---|---|
| Distinct verbs invoked from `E` | ≥ 50% of `E`'s verbs at current tier |
| Total successful invocations of `E`'s verbs | ≥ 5 |
| Time since first encounter | ≥ 24 hours |
| Last invocation of `E` failed | False |

Global tier advances when ≥ 2 extensions have advanced past the current
global tier. Both advancements are silent — the user sees the new verbs
appear in `axi help` and tab-completion without ceremony. A one-time
post-graduation hint surfaces ("you've reached `advanced` for `tidy` —
3 new verbs are now suggested").

Direct invocation of an `advanced` or `internal` verb (when the user
types the full path) is allowed at any tier; doing so successfully
counts as a graduation signal for that extension.

### Intent groups

A verb may belong to multiple intent groups (declared in the manifest as
`intent_groups = ["maintain", "investigate"]`). The platform-recognized
groups are: `start`, `teach`, `maintain`, `investigate`, `build`,
`govern`, `research`. Extensions may declare additional ad-hoc groups,
which surface only via explicit `axi help <group>` invocation.

`axi help <group>` rolls up verbs from all extensions tagged with that
group, filtered by the user's current effective surfacing rule.

---

## CLI History and Decay

The shared history stream backs both *Smart help* (PRD) and *Chat ↔ CLI
parity* (PRD). It is a single append-only event log of every `axi`
invocation, regardless of whether issued from a terminal or a chat.

### Event schema

Each entry is a JSON object:

```json
{
  "ts": "2026-05-02T19:41:13.123Z",
  "noun": "tidy",
  "verb": "worktrees",
  "args": ["--prune"],
  "exit": 0,
  "duration_ms": 142,
  "cwd": "/Users/example/Projects/workspace/axiom",
  "user": "@laptop:ben",
  "via": "cli",                          // "cli" | "chat" | "remote-trigger"
  "extension": "hygiene"
}
```

`args` is redacted before write per a denylist + per-verb hint
(positionals known to contain credentials are stripped). The redaction
ruleset is configurable; see `spec-axi-cli.md §Helpful Errors` for the
similar redaction used in error reporting.

### Memory-provider abstraction

The history stream lives behind a provider interface so the slim install
mode and the full Axiom Memory mode share one consumer surface:

| Provider | When | Notes |
|---|---|---|
| `axiom_memory_episodic` | Default for full installs | Writes via `CompositionService` into MIRIX `episodic` tier; benefits from automatic decay-via-tier-transition (episodic → semantic → archive) |
| `sqlite_history` | Slim install fallback | Single table `cli_history` in the install-tier SQLite database (always present); decay via scheduled DELETE-by-age |

A user can override the provider with the `AXI_HISTORY_PROVIDER` env var
or in `~/.axi/config.toml`.

### Decay model

History entries are first-class **only for a configurable window**
(default: 30 days at full fidelity). Older entries are not deleted but
are progressively consolidated:

1. **30+ days old** — verbose `args` are dropped; only `noun verb`
   summary is retained.
2. **90+ days old** — individual entries roll up into per-verb counters
   (frequency, last-seen) plus inferred patterns ("user typically runs
   `classroom prep` on Mondays"). The original entries can then be
   archived or dropped.
3. **Adaptive curve** — the system observes which historical detail
   most often proves useful when reconstructing chat context, and tunes
   the decay schedule over time. The default schedule above is the
   bootstrap; long-running deployments evolve their own.

The retention windows are exposed via `axi config history` for users who
want to tighten or relax them.

### Hand-off contract

When `axi chat` starts, it ingests the most recent N entries (default:
last 6 hours, capped at 50 entries) into its system context as
"things the user just did." This is the substrate for the *hand-off*
property documented in the PRD. The user can disable ingestion per
session with `axi chat --no-history`.

---

## Chat ↔ CLI Resolution

Chat is **not restricted** to invoking only verbs that have a CLI
equivalent. It is an LLM-mediated agent with access to its full tool
surface — Axiom's CLI verbs, attached MCP tools (Linear, Calendar,
custom domain MCPs, etc.), and the ability to compose multi-step
sequences across all of those. What this section specifies is the
**gravity** — the bias chat applies when a CLI command does cover
the user's intent — and the **provenance discipline** that keeps the
two sources of action distinguishable in the audit trail.

### CLI gravity (the bias toward determinism + compliance)

When a user's intent maps to an existing `axi <noun> <verb>`
invocation, chat strongly prefers resolving to that CLI command
rather than synthesizing an equivalent sequence of tool calls. The
bias is intentional:

- **Determinism.** A CLI verb has known semantics, fixed argument
  shape, and reproducible behavior across runs. An LLM-composed
  sequence of MCP tool calls does not.
- **Compliance + auditability.** CLI invocations land in the unified
  history stream with `via=chat` provenance, the verb's own logs,
  and the platform's RACI ledger. The same intent expressed as ad-hoc
  tool calls is harder to audit and harder to attribute.
- **Performance.** A CLI verb that already wraps the right
  optimizations (caching, batching, federation routing) outperforms
  the LLM re-discovering them at runtime.
- **Tier respect.** CLI verbs carry the surfacing tier the user has
  earned; ad-hoc tool calls bypass that signal.

In practice: when chat's intent-parser identifies a high-confidence
match to a CLI verb, it surfaces the resolved invocation in the
preview frame ("I'll run: `axi hygiene list worktrees --prune` …"), executes
through `axi.run(...)` (the same dispatch path a terminal invocation
uses), and records it in history with `via=chat`.

### Resolution path

```
parse_intent(user_text)
  → ResolvedAction (one of):
      a) CliInvocation  { noun, verb, args }                    # high confidence; CLI gravity
      b) CliSequence    { steps: [CliInvocation, …] }           # multi-step CLI plan
      c) MixedPlan      { steps: [CliInvocation | ToolCall, …] }# CLI + MCP tool calls
      d) ToolOnly       { steps: [ToolCall, …] }                # no CLI surface for this; pure tool use
      e) NoAction       (intent doesn't map; ask the user to clarify)

  → if any step is internal-tier or contains a destructive write:
      ask the user to confirm or clarify per RACI rules
  → display the resolved plan to the user (each step shown with the
    literal CLI invocation or tool call it represents)
  → confirm
  → dispatch:
      - CliInvocation steps go through `axi.run(...)` — same path as terminal
      - ToolCall steps go through the tool's normal handler
  → record in history; each step carries its own `via=` and provenance
  → render result with the same formatter as the CLI for CliInvocation
    steps; tool-native rendering for ToolCall steps
```

`parse_intent` is grounded in the noun-verb tree the help system
surfaces — chat respects the user's current tier and per-extension
familiarity when proposing CLI verbs. It does not silently invoke
`internal`-tier verbs even when the LLM thinks they apply (the user
is asked to confirm and shown the tier elevation explicitly, per
the §Reveal mechanism above).

### When chat composes beyond the CLI surface

Three legitimate reasons chat may produce a `MixedPlan` or `ToolOnly`
plan rather than a single `CliInvocation`:

1. **No CLI command covers the intent.** The user asks chat to draft
   a Slack message, query Linear for issues mentioning a topic, or
   fetch a Calendar event. These are MCP tool surfaces; no CLI verb
   wraps them yet. Chat uses the tool directly.
2. **The intent spans multiple CLI verbs in a known pattern.** "Prep
   a fresh review session" might compose `axi hygiene list worktrees --prune`
   + `axi review --base main` + `axi pub status`. Each step is a
   first-class CLI invocation; chat is the planner that orders them.
3. **The intent mixes CLI work with external tools.** "Open an issue
   for the failures RIVET flagged this morning" composes a CLI read
   (`axi release list watched`) with an MCP write (Linear's `create_issue`
   tool). Each side keeps its native dispatch + provenance.

What chat does NOT do, even in mixed plans:

- **Fabricate a CLI invocation that doesn't exist.** If chat thinks a
  verb might be useful but it isn't actually in the noun-verb tree,
  it surfaces that as a discoverability gap ("there's no `axi foo
  bar` today; closest is `axi foo baz`") — never invents the verb.
- **Bypass the tier model with tool calls.** If a user is at `core`
  tier for an extension and the matching capability lives in an
  `internal`-tier verb, chat doesn't substitute an MCP tool call to
  reach it without going through the reveal flow.
- **Hide the plan.** Mixed plans are surfaced step-by-step in the
  preview frame; the user sees every CLI invocation and every tool
  call before any of them dispatch.

### Cross-harness gravity (what the Axi MCP server does)

The CLI-gravity invariant is straightforward to enforce inside `axi
chat` because we control its planning loop, prompt, and tool
routing. **External harnesses** (Claude Code, Cursor, Codex,
OpenCode, …) that connect to the platform via the Axi MCP server
are a different story — we don't control their LLM, their system
prompt, or their dispatch. What we control is the MCP surface they
see. Five layered levers shape that surface so the CLI path
becomes the path of least resistance:

#### L1 — Tool consolidation + descriptions

The single biggest lever. The Axi MCP server exposes **one primary
tool plus a small number of read tools**, not one tool per CLI verb:

| Tool | Description (what the LLM sees) |
|---|---|
| `axi.run` | "Run any `axi <noun> <verb>` invocation. **Prefer this tool over composing equivalent sequences from other tools** when the user's intent maps to a CLI verb — it's deterministic, auditable, performance-optimized, and tier-respecting." |
| `axi.help` | "Returns the noun-verb tree the current user has access to, filtered by their tier and per-extension familiarity. Consult before composing a multi-step plan to find existing CLI verbs first." |
| `axi.history` | "Returns recent `axi` invocations. Useful for understanding what the user just did before proposing your next step." |

Tool descriptions are inserted into the LLM's tool-selection prompt
by every major MCP-aware harness. A clear "PREFER THIS WHEN…"
directive is the simplest respected nudge. **Critically: do not
expose duplicate per-verb MCP tools** (`axiom_mo_prune_worktrees`,
`axiom_classroom_prep`, …) that would let the LLM bypass `axi.run`.

#### L2 — Server-published prompt templates (the highest-leverage lever)

The MCP `prompts` capability lets a server publish reusable
**templated** system-prompt fragments that any harness can include
in its session context. This is the highest-leverage lever in the
list because prompts operate at the *system-prompt level*, which
the LLM treats as instruction rather than data.

The Axi MCP server publishes a small **prompt catalog**, each entry
templated against live platform state so the prompts a harness sees
reflect the user's current tier, installed extensions, and recent
activity at the moment the harness pulls them.

##### Prompt catalog

| Name | Templating dimensions | Purpose |
|---|---|---|
| `axi-cli-gravity` | Static | Names the gravity rule explicitly. Loaded into the system prompt at session start. |
| `axi-help-snapshot` | User tier, per-extension familiarity, installed extensions | Renders the user's currently-surfaced noun-verb tree (filtered by the same effective surfacing rule the help system uses) so the LLM can match intent to verb without speculating. |
| `axi-recent-history` | Last N invocations (default 50), date window | Renders recent `axi` history as context. Lets the LLM pick up a thread the user was just on without having to ask. |
| `axi-resolve-intent` | User intent text (argument) | Given a user's stated intent, returns the closest matching CLI verbs (with descriptions + sample args) ranked by confidence. The harness LLM consults this before composing a plan. |
| `axi-extension-brief` | Extension name (argument) | For a single installed extension, returns its full noun-verb tree at the user's familiarity level + a one-paragraph "what this extension is for." Lets the LLM correctly invoke unfamiliar extensions without ad-hoc exploration. |
| `axi-tier-of-verb` | Verb (argument), user state | Tells the harness whether a verb the LLM is considering is at the user's tier — preventing tier-bypass via tool calls. |

##### Templating wire shape

Each prompt is published per the MCP `prompts/list` and `prompts/get`
methods. A `prompts/get` call carries the prompt name + arguments
and returns rendered content the harness can splice into the
session:

```json
// harness → server
{
  "method": "prompts/get",
  "params": {
    "name": "axi-help-snapshot",
    "arguments": {
      "include_tier": "core",
      "extensions": ["hygiene", "release", "classroom"]
    }
  }
}

// server → harness
{
  "messages": [
    {
      "role": "system",
      "content": {
        "type": "text",
        "text": "Available `axi` verbs at user's current tier (core):\n\nhygiene: status, clean, ls, stat, …\nrelease: status, watch, watched, plan, …\n…"
      }
    }
  ]
}
```

The Axi server's prompt-renderer reads from the same in-process
`ExtensionRegistry` and `competency.json` that the CLI dispatcher
uses, so prompts are always consistent with what `axi help` would
show the user at that moment.

##### Refresh + invalidation

When the underlying platform state changes (extension installed /
upgraded / removed; user's tier graduates; competency state pinned
explicitly), the Axi server emits the MCP
`notifications/prompts/list_changed` notification. Conformant
harnesses re-fetch the affected prompts on their next interaction.
The same `extensions.changed` event from
[`spec-extension-loading.md §4.2`](spec-extension-loading.md) drives
this — prompt invalidation rides the existing hot-load substrate.

##### Recommended harness usage (descriptive, not enforced)

| At … | The harness should pull … |
|---|---|
| Session start | `axi-cli-gravity` + `axi-help-snapshot` |
| User asks an action question | `axi-resolve-intent(intent=user_text)` |
| User invokes an unfamiliar extension | `axi-extension-brief(extension=<name>)` |
| User picks up where they left off | `axi-recent-history(window=24h)` |
| LLM is about to call a duplicate-of-CLI MCP tool | `axi-tier-of-verb(verb=<inferred>)` |

We can't *force* a harness to follow these patterns, but we can
ship the patterns as part of the cross-harness shim generator's
output (`axi commands generate`) — each harness's generated
config-file block mentions the recommended prompt-pull points in a
comment block, so users + harness LLMs see the recipe.

##### Versioning

Each prompt carries a `version` in its metadata (e.g. `axi-cli-gravity v1.2`).
Breaking changes bump the major version. Harnesses that pin a major
version stay stable across server upgrades; harnesses that follow
HEAD get the newest content automatically. The spec for the version
field follows the AEOS `aeos_version` field shape so the conventions
match.

##### Extension contributions (AEOS `kind = "prompt"`)

Installed extensions can **contribute** their own prompts to the
catalog *and* extend platform prompts via declared fill points,
through the AEOS `kind = "prompt"` capability (per
[`spec-aeos-0.1.md §4.8`](spec-aeos-0.1.md#48-prompt)).

Two contribution shapes:

1. **Standalone prompts**, namespaced by extension. A `classroom`
   extension might publish `classroom.grading-context` for an
   instructor's grading workflow. The harness lists it alongside
   `axi-*` platform prompts in the `prompts/list` response.

2. **Extending a platform prompt** via `extends` + `fill_point`. A
   classroom extension might extend `axi-help-snapshot` at the
   `extension_context` fill point to add active-cohort information
   when the user is in a classroom session. The platform prompt
   declares fill points; extensions register content for them; the
   renderer composes at request time.

###### Renderer composition algorithm

When the harness calls `prompts/get` for a platform prompt that
has fill points, the renderer:

1. Loads the parent template
2. For each fill point, collects every installed extension's
   contribution that targets it (per the extension's manifest)
3. **Filters by surfacing rule** — only contributions from
   extensions the user has reached at the contribution's declared
   `tier` are included (per [§Capability Tiers](#capability-tiers-and-familiarity-tracking))
4. Sorts contributions alphabetically by extension name
   (deterministic; consistent with the rest of the platform's
   conflict resolution)
5. Substitutes platform-context variables (`{{ context.user.tier }}`,
   `{{ context.installed_extensions }}`, etc.) and argument values
6. Renders provenance markers around each contribution:
   `<!-- contributed-by: classroom (v1.2.0) -->` ... `<!-- end -->`
7. Returns the composed result

###### Structural enforcement

We can't enforce the *natural-language quality* of extension
contributions, but we can enforce structure. `axi ext lint`
verifies, at install time:

| Check | Enforced? |
|---|---|
| Manifest schema validity | ✅ AEOS validator |
| Templating syntax parses | ✅ |
| All `{{ arguments.* }}` resolve to declared arguments | ✅ |
| All `{{ context.* }}` resolve to the platform-context schema | ✅ |
| Body length within per-tier ceiling | ✅ |
| Provenance marker present | ✅ |
| Fill-point exists on the parent prompt | ✅ |
| Contribution doesn't claim verbs the extension doesn't ship | ✅ (best-effort lint) |
| Contribution doesn't contradict `axi-cli-gravity` directive | ⚠️ heuristic only |
| Natural-language quality / clarity / tone | ❌ out of reach |

The structural enforcement covers what matters for safety + drift-
prevention. The natural-language quality is a community concern,
not a schema concern — we ship the lint checks that *can* fire
mechanically, and trust extension authors with the rest.

###### What extensions cannot do

- **Inject content outside declared fill points.** The parent
  prompt's authors decide where extension content can land. An
  extension that wants to add content somewhere the parent doesn't
  invite must propose a new fill point upstream.
- **Override platform prompts.** `extends = "axi-cli-gravity"` is
  permitted to *augment* (within fill points), never to *replace*.
  Replacement is a capability we deliberately don't expose; the
  gravity directive is platform-load-bearing.
- **Mask other extensions' contributions.** Composition is additive
  and deterministic; one extension cannot suppress another's
  contribution. Conflicts at the same fill point are resolved by
  alphabetical-by-extension order with a single ledger entry; both
  contributions are rendered with their provenance markers intact.

#### L3 — Generated slash-command shims

The cross-harness shim generator (per [`prd-commands-generator.md`](../prds/prd-commands-generator.md))
emits per-verb shims that route the user's typed input directly
to `axi.run` — gravity at the user's keystroke level, not just at
the LLM's planning level. When a user types `/axi hygiene list worktrees --prune`
in Claude Code, the shim dispatches through `axi.run`
without involving the LLM's planning loop at all.

#### L4 — Server-side advisory results

When the harness LLM calls a generic MCP tool (`linear.create_issue`,
shell tool, etc.) and the Axi server detects an `axi` verb covers
the same intent, the server returns the tool result *with* an
advisory header:

```json
{
  "result": "...",
  "axi_advisory": {
    "suggested": "axi <noun> <verb> ...",
    "reason": "this matches a known CLI verb; future invocations should prefer it",
    "auditable": true
  }
}
```

The advisory enters the LLM's context and biases the *next* turn
toward `axi.run`. Soft signal, but soft signals compound across a
session.

#### L5 — Sampling + elicitation (when MCP capabilities mature)

Newer MCP capabilities let a server ask the user (`elicitation`) or
the LLM (`sampling`) on its own initiative. When the receiving
harness supports them, the Axi server intercepts a synthesizing
tool call and asks: *"This looks like `axi hygiene list worktrees --prune`.
Want me to run that instead?"* — closer to enforcement, available
only when the harness implements the capability.

#### What's NOT enforceable (and why honesty about this matters)

The Axi MCP server **cannot prevent** a determined LLM in another
harness from synthesizing an ad-hoc sequence. We don't control the
LLM, the harness's system prompt, or the harness's dispatch. We can
only:

- Make CLI gravity **attractive** (best descriptions, cleanest
  shims, server-published prompts)
- Make ad-hoc paths **less attractive** (no duplicate per-verb MCP
  tools)
- Make the choice **visible** (the provenance discipline below
  surfaces "what did chat synthesize that wasn't a CLI invocation?"
  as an audit query across all harnesses, so drift is detectable
  even when not preventable)

This is *engineered gravity*, not *enforced parity*. The distinction
matters: we earn gravity through MCP-surface design choices, and we
detect drift through provenance — we don't pretend to a control
surface we don't have.

### Provenance discipline (the invariant that keeps chat from drifting into a parallel API)

The `via=` field on each history entry distinguishes:

| Value | Meaning |
|---|---|
| `cli` | Direct terminal invocation |
| `chat` | Chat-resolved CLI invocation |
| `chat:tool` | Chat-mediated MCP tool call |
| `chat:plan(N/M)` | Step N of M in a chat-resolved sequence |
| `remote-trigger` | Cloud routine invocation |

This makes it possible to query the history for "what did chat
synthesize that *wasn't* a CLI invocation?" — a useful audit handle
when adding a new CLI verb is being considered, and a useful safety
handle when investigating drift.

The drift-prevention invariant is therefore not "chat invokes only
CLI verbs" (too strict; it cripples chat). It is:

> **Whenever a CLI verb covers the intent, chat resolves to it.**
> When chat composes beyond the CLI surface, every step is shown to
> the user, every step's provenance is recorded, and every CLI step
> goes through the same dispatch path the terminal uses. The CLI
> remains the canonical surface; chat is the surface that *reaches*
> it the most flexibly.

---

## Chat Mode

> **Reading guide:** This section catalogs the **currently shipped**
> chat capabilities and slash-command surface. The **resolution model**
> chat uses to map fuzzy intent to literal `axi <noun> <verb>`
> invocations — the spec for how chat behaves going forward — is in
> [§Chat ↔ CLI Resolution](#chat--cli-resolution) above. When the two
> overlap, the resolution model is normative; this section is
> descriptive of the current implementation.

`axi chat` launches an interactive agentic session — an LLM-powered assistant with tool calling, RAG context injection, and multi-provider routing.

### Capabilities

| Capability | Description |
|------------|-------------|
| **Tool calling** | Multi-turn tool-use loop (up to 10 rounds) via Gateway |
| **RAG grounding** | Automatic context injection from pgvector knowledge base |
| **Tier routing** | Keyword + SLM classification routes to appropriate LLM tier |
| **Streaming** | First round streams; subsequent rounds (after tools) are non-streaming |
| **Approval gates** | Write operations require human confirmation |
| **Session persistence** | PostgreSQL-backed sessions with resume support |
| **Workspace awareness** | Reads CLAUDE.md, model.yaml, personal context |
| **Slash commands** | `/signal`, `/pub`, `/save`, `/model`, `/sessions`, etc. |

### Chat Tools

These tools are available to the LLM during chat. Read tools auto-execute; write tools require approval.

**Read-only (auto-approved):**

| Tool | Description |
|------|-------------|
| `query_docs` | Check publisher status of tracked documents |
| `signal_status` | Show inbox/processed/draft counts |
| `list_providers` | List all registered publisher providers |
| `doc_check_links` | Verify cross-document link resolution |
| `doc_diff` | Show documents changed since last publish |

**Write (require approval):**

| Tool | Description |
|------|-------------|
| `signal_ingest` | Run extractors on inbox data |
| `doc_generate` | Generate .docx from markdown |
| `doc_publish` | Generate and publish document to storage |
| `write_file` | Write content to file |
| `write_inbox_note` | Drop text note into signal inbox |

Tools are extensible: core extensions register tools via `tools_ext/` hot-reloading, user extensions via `discover_and_load_chat_tools()`.

### Slash Commands

Slash commands are in-chat meta-commands. They run locally (no LLM call) and dispatch real CLI commands under the hood.

**Session management:**

| Command | Description |
|---------|-------------|
| `/sessions` | Browse and manage sessions |
| `/sessions rename <title>` | Rename current session |
| `/sessions archive [id]` | Archive session(s) |
| `/resume <id\|#>` | Load a session by ID or number |
| `/new` | Start a fresh session |
| `/clear` | Clear message history |
| `/compact` | Summarize conversation to save tokens |

**Chat control:**

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/status` | Session info, gateway, usage |
| `/usage` | Token usage and cost breakdown |
| `/model` | Switch LLM provider mid-chat |
| `/context` | Show context window usage |
| `/save` | Save last response to knowledge corpus |
| `/doctor` | Quick health check |
| `/update` | Check for updates |
| `/exit` | Save and exit |

**Signal commands** (forwarded to SCAN agent):

| Command | Description |
|---------|-------------|
| `/signal brief` | Catch up on what happened |
| `/signal media` | Search, play, discuss recordings |
| `/signal draft` | Generate weekly changelog |
| `/signal status` | Pipeline health |
| `/signal pipeline <cmd>` | Advanced pipeline operations |

**Publisher commands** (forwarded to PRESS agent):

| Command | Description |
|---------|-------------|
| `/pub overview` | Document ecosystem dashboard |
| `/pub publish` | Generate + publish |
| `/pub review` | Interactive review |
| `/pub status` | Document status |
| `/pub diff` | Changed docs since last publish |

**Hidden aliases:** `/sense` → `/signal`, `/doc` → `/pub`

### Session Management

Sessions are stored in PostgreSQL (see [spec-session-store.md](spec-session-store.md)). This enables resume, multi-client access, and interaction logging.

```bash
# Resume most recent session
axi chat --resume

# Resume a specific session
axi chat --session abc123def456

# List recent sessions
axi chat --list

# Sync local JSON sessions to PG
axi chat --sync
```

**`axi chat --list` output:**

```
Recent sessions:
  ID            Title                          Messages  Cost     Last Active
  abc123def456  Quarterly status review        12        $0.34    2 min ago
  def456abc123  Pipeline failure debugging     47        $1.20    3 hours ago
  789012345678  Onboarding handoff             8         $0.00    yesterday
```

Sessions fall back to local JSON files when PostgreSQL is unreachable. See [spec-session-store.md §8](spec-session-store.md) for graceful degradation.

### Tool Modes

By default, all tools are available. Tool modes restrict the tool set for safety or simplicity:

```bash
# Read-only tools only (query, search, status — no writes)
axi chat --simple

# Specific tools only
axi chat --tools rag,model

# Full tool access (default)
axi chat
```

`--simple` is recommended for new users and read-only exploration sessions.

### Token Budget

Sessions can have a token budget ceiling to prevent runaway tool loops:

```bash
# Set budget for this session
axi chat --budget 100000

# Configure default budget globally
axi config set chat.max_budget_tokens 200000
```

When the budget is exhausted, the agent stops gracefully.

### Cost Display

Every turn shows token usage and estimated cost:

```
> summarize the latest log entries

  In the last 24 hours: 3 status checks (all passing), 1 backfill
  completed, 0 alerts...

  ─── 1,234 in / 567 out · $0.02 this turn · $0.15 session ───
```

Self-hosted models (e.g. Qwen on a local llama-server) show `$0.00`.
Hosted-API models (e.g. Claude) show their real per-call cost.

### Structured Output

```bash
# Return JSON instead of prose
axi chat "list all models tagged 'production'" --json

# Pipe to jq
axi chat "what materials are tracked?" --json | jq '.materials[]'
```

`--json` suppresses streaming, rich rendering, and the cost footer.

### Safety Boundaries

- **Read-heavy**: Most operations are queries and analysis
- **Human-in-loop**: Writes require confirmation via ApprovalGate
- **Audit trail**: All actions logged with session ID in PostgreSQL
- **Scope limits**: Cannot reach systems outside the configured authority/cohort
- **Tool modes**: `--simple` restricts to read-only tools

---

## Rendering Architecture

Two-layer abstraction for terminal output:

| Layer | Purpose |
|-------|---------|
| **RenderProvider** (ABC) | Terminal rendering: streaming, tool results, approvals, status |
| **InputProvider** (ABC) | User input: prompts, history, tab completion |

**Concrete implementations:**

| Provider | Description | Dependency |
|----------|-------------|-----------|
| `AnsiRenderProvider` | Zero-dependency ANSI escape codes | None (stdlib) |
| `RichRenderProvider` | Enhanced output with syntax highlighting | `rich` |
| `PtkInputProvider` | Full readline, history, completion | `prompt_toolkit` |
| `BasicInputProvider` | Fallback `input()` | None (stdlib) |

Selection is automatic (rich if available, else ANSI) or forced via `--render` / `--input` flags.

---

## UI Affordances and Conventions

> **Reading guide.** The *Rendering Architecture* section above
> specifies the primitives (which library renders what). This section
> specifies the **user-facing affordances** built on top of those
> primitives — what a user encounters, where, why it lives there,
> when it appears, what behavior change it causes, and which code
> path implements it. It is the spec for our UX surface as a coherent
> system, not a list of widgets.

### What an "affordance" is here

A **CLI affordance** is a discoverable interaction unit a user encounters
during a session — a slash command, an approval prompt, a tier-graduation
hint, a "did you mean" suggestion, a hot-load notification, a status
footer line. Each affordance has five attributes we hold ourselves to:

1. **Placement** — which surface (terminal, chat REPL, cross-harness
   shim, background notification stream) it lives on, and why.
2. **Dynamism** — when it appears, when it doesn't, what triggers it.
3. **UX impact** — the user-behavior change it's designed to cause.
4. **Underlying function** — which module / event / state object it
   reads or writes.
5. **Status** — shipped, partial, or designed (per
   [`prd-axi-cli.md §Status & Phasing`](../prds/prd-axi-cli.md#status--phasing)).

Treating these five attributes as load-bearing for every affordance
keeps the surface coherent as it grows.

### Surfaces

The CLI exposes affordances on four surfaces, each with different
constraints:

| Surface | Where the user is | Constraints |
|---|---|---|
| **Terminal** | Direct `axi <noun> <verb>` shell invocation | One-shot; no persistent state between invocations beyond stdout/stderr/exit code; tab-completion lives here |
| **Chat REPL** | `axi chat` interactive session | Persistent state across turns; full ANSI/Rich available; can use multi-line approval frames, toolbars, status footers |
| **Cross-harness shims** | Claude Code, Cursor, VS Code, Codex, OpenCode, Vim, Neovim | Fidelity bounded by the harness — we don't control its rendering. See [`prd-commands-generator.md`](../prds/prd-commands-generator.md) |
| **Background notification stream** | Daemons (RIVET, TIDY, SCAN, TRIAGE) writing to log files / pushing to status bars | No interactive prompt available; affordances must be readable in retrospect |

A given affordance may live on one surface (e.g. tab-completion is
terminal-only) or compose across several (e.g. the resolution-preview
appears in chat *and* its formatter is reused for the terminal "did
you mean" suggestion).

### Affordance catalog

#### Discovery & navigation

| Affordance | Surface | Status | Placement rationale | Dynamism | UX impact | Underlying function |
|---|---|---|---|---|---|---|
| Tab completion | Terminal | 🟡 Partial — argcomplete shipped, tier-aware filtering 🟦 designed | Discovery happens at keystroke time; the shell is the only place that observation can be cheap | Always-on for installed shells; respects the user's surfacing rule | User never tabs into a verb above their tier | `axi completions` (designed); `extensions.discovery.discover_command_tree()` |
| `axi help` (smart) | Terminal + chat | 🟦 Designed → Phase 1 | Help is the deliberate-discovery entry point; must reflect tier + history | Adapts per user (current tier, recent invocations, cwd, common chains) | Novice not flooded; returning user sees next-step verbs | `competency.json` + history reader |
| `axi help <intent-group>` | Terminal | 🟦 Designed → Phase 1 | Cross-extension rollups belong at help time, not buried | Static per release of the manifest set | User finds verbs by task, not by extension boundary | `intent_groups` field on `kind=cmd` blocks |
| Tier-graduation hint | Terminal (post-help) | 🟦 Designed → Phase 1 | Surface the elevation the moment it earns | One-time per graduation event | User notices new verbs without ceremony | competency-state advance hook |
| Reveal mechanism (`--all`, tab-twice, chat surfacing, `--pin`) | Terminal + chat REPL | 🟦 Designed → Phase 1 | Discovery being filtered must come with a non-blocking widening; experts shouldn't wait for graduation thresholds | On user request (flag), tab-twice, chat-detected hidden-verb resolution, or one-time post-install hint | Senior user gets the wide surface immediately; reveal hints stop appearing once familiarity catches up | `axi help --all` flag; `axi competency set/unset`; tab-twice handler; chat resolution-preview extension; reveal entries logged to history with `via=reveal` for graduation-observer feedback |
| Did-you-mean suggestion | Terminal + chat | 🟡 Partial — fuzzy match shipped (`find_close_command`); context-aware enrichment 🟦 designed | Belongs at error time, not in help | Per error, scored against installed verbs + cwd context | User recovers from typos quickly | `chat/commands.py:find_close_command`; designed to extend with cwd signals |
| Welcome banner | Chat REPL (entry) | 🟡 Partial — banner shipped (T1.7); AXI self-identification 🟦 designed → Phase 1 | Session-start is the only moment to set tool/agent context (`axi v… · sonnet via gateway · cwd …` + "AXI here.") | One-time per chat session | User knows what tool they're in *and* who they're talking to (closes the "people think `axi` is the agent" gap) | `renderer.py:render_welcome` |
| Agent speaker prefix (`AXI ▸`, `TIDY ▸`, …) | Chat REPL | 🟦 Designed → Phase 1 | The text in the box must be signed; otherwise the box's label (the tool name) leaks onto the speaker | Per agent turn | User sees who's talking, not just "text from `axi`" | `renderer.py:render_message` (extend) |
| Agent direct-address (`@axi`, `@tidy`, …) | Chat REPL | 🟦 Designed → Phase 1 | Explicit address belongs in the same surface as the conversation | When the user types `@<agent>` as the first token of a turn | User can route around the orchestrator's default routing | Chat resolution path; agent registry |
| Federated agent address (`@<agent>:<server>`) | Chat REPL + terminal | 🟦 Designed → Phase 2 | Federation traffic must reuse the platform's existing principal-naming rule | When the user includes a peer-node agent in the session | User addresses cross-node agents with the same syntax as cross-node humans | A2A protocol; federation routing |
| `@`-handle tab-completion (three cascading sublists: entity, server-suffix, scoped-on-server) | Chat REPL + terminal | 🟦 Designed → Phase 2 | Discoverability is the gating concern for any addressing scheme; making it federation-aware from the start avoids a v2 retrofit | On `<Tab>` after `@`, after `@<entity>`, or after `@:<server>` | User finds peers + agents without remembering exact handles; same surface scales to P2P chat invites | Federation peer registry + cohort registry + RACI visibility filter; cached locally with TTL |

#### Action confirmation & approval

| Affordance | Surface | Status | Placement rationale | Dynamism | UX impact | Underlying function |
|---|---|---|---|---|---|---|
| Slash command (chat meta) | Chat REPL | ✅ Shipped — 40+ verbs | In-chat meta-control belongs in chat, not as terminal verbs | Always available mid-session | User controls session without leaving chat | `chat/commands.py:get_slash_commands` |
| Approval corner-frame | Chat REPL (write actions) | ✅ Shipped (T4.10) | Writes need the strongest visual stop sign; box-drawing scales to terminal width | Per write-action proposal | User can't accidentally approve through inattention | `renderer.py:render_approval_prompt` |
| `[y/N]` destructive confirm | Terminal | ✅ Shipped | Destructive verbs live in terminal; the prompt is the last stop before commit | Per invocation of an `advanced`/`internal` verb (designed: also tier-conditional for `core`) | User pauses on irreversible operations | `infra/orchestrator/actions.py` |
| Per-tool allowlist (`/permissions`) | Chat REPL | ✅ Shipped (T1.3) | Per-session, per-tool granularity needs an in-session edit surface | Updates between turns; persists for session | User can pre-approve repetitive tool patterns | `chat/agent.py` allowlist + `commands.py:cmd_permissions` |
| Resolution preview ("I'll run: …") | Chat REPL | 🟦 Designed → Phase 2 | The chat→CLI boundary is exactly where parity becomes visible | Per chat-resolved action, before dispatch | User trusts chat (sees the literal CLI invocation) | Chat resolution path (per `§Chat ↔ CLI Resolution`) |
| Restart-recommended notice | Chat REPL + terminal | 🟦 Designed → Phase 2 | When hot-swap can't be safe, the user must be told before the next action | Per non-hot-swappable extension change | User isn't surprised by stale state | `extensions.changed` event; `spec-extension-loading.md §5.3` |

#### Status, history, and provenance

| Affordance | Surface | Status | Placement rationale | Dynamism | UX impact | Underlying function |
|---|---|---|---|---|---|---|
| Toolbar (mode · model · provider) | Chat REPL | ✅ Shipped | Mode + model are continuous-context info; persistent toolbar is cheaper than re-asking | Updates on `/mode`, `/model`, provider switch | User always knows the autonomy + cost surface they're in | `chat/providers/ptk_input.py:_build_toolbar` |
| Cost / token footer | Chat REPL | ✅ Shipped | Cost feedback belongs adjacent to the action that incurred it | Per turn | User understands cost cadence | `chat/agent.py` token accounting + `renderer.py` footer |
| Session list (`/sessions`) | Chat REPL | ✅ Shipped | Browsing history lives in the chat that produced it | On demand | User resumes intent without losing thread | `commands.py:cmd_sessions` + `SessionStore` |
| Hand-off context ingest | Chat REPL (entry) | 🟦 Designed → Phase 2 | Session start is when "what was I doing?" matters most | Per chat session start (last 6h, last 50 entries) | User hands work to chat without re-explaining | CLI history reader + chat system-prompt composer |
| Streaming output + spinner | Chat REPL + terminal | ✅ Shipped | Long-running calls need liveness signal; spinner is the established convention | Per LLM call / long-running verb | User knows the system isn't stuck | `renderer.py:stream_text` |

#### Hot-load & install lifecycle

| Affordance | Surface | Status | Placement rationale | Dynamism | UX impact | Underlying function |
|---|---|---|---|---|---|---|
| Hot-load notification ("✓ N new verbs available now") | Terminal (post-`axi ext install`) | 🟦 Designed → Phase 2 | Right after the install verb is when the user expects feedback | One-shot per install | User knows they don't have to restart | `extensions.changed` event subscriber in `axi_cli.py` dispatcher |
| Cross-harness shim refresh notice | Terminal (post-`axi update`) | 🟡 Partial — call shipped (#113); UX line 🟦 designed | Belongs in the verb's output the user just ran | One-shot per update with previously-generated state | User trusts shims stay in sync | `axi update` post-hook → `commands.regenerate_all()` |
| First-run completion install prompt | Terminal | 🟦 Designed → Phase 2 | First-run is the only moment this consent question fits naturally | Once per fresh install | User gets working completion immediately | `axi completions install` first-run path |
| Update-available banner | Terminal (next `axi` invocation) | ✅ Shipped | Non-blocking nudge between commands is the only place it doesn't interrupt work | Per `axi` invocation when newer version detected (rate-limited) | User updates on their cadence | `axiom_cli.py:_check_and_prompt_update` |

#### Reporting & evidence

| Affordance | Surface | Status | Placement rationale | Dynamism | UX impact | Underlying function |
|---|---|---|---|---|---|---|
| Helpful error (3-part shape) | Terminal + chat | ✅ Shipped (T1.7) | Errors are where users are most stuck — the format earns its space | Per failed invocation | User has a next step, not just a stack trace | `chat/errors.py:friendly()` mapper |
| Severity-tagged finding (REV-U, TIDY verdicts) | Terminal | ✅ Shipped (#108, #109) | Reports with mixed severity benefit from grouping | Per report invocation | User triages by severity quickly | `review/agents/rev_u/` formatter; `hygiene/worktrees.py:StalenessVerdict.reasons` |
| Conflict report (`axi commands list --conflicts`) | Terminal | ✅ Shipped (#113) | Surfacing shadow definitions belongs in the same verb that surfaces winners | On demand | User understands the deterministic resolution | `commands/discovery.py:Conflict` |
| `axi memory show` rendering | Terminal | 🟡 Partial — markdown rendered as plain ANSI today | Memory inspection lives in the verb that owns memory | Per invocation | User reads memory cleanly | `extensions/builtins/memory/cli.py` |

#### Sci-display surfaces (envisioned)

| Affordance | Surface | Status | Placement rationale | Dynamism | UX impact | Underlying function |
|---|---|---|---|---|---|---|
| Inline ASCII plot | Terminal + chat | 🟦 Designed | Inline-in-the-scroll is the right shape for "glance and continue" | Per result that includes numeric series | User sees the data without context-switching | scidisplay/ extension (currently stubbed) |
| Equation rendering (terminal unicode + browser MathJax fallback) | Terminal + chat | 🟦 Designed | Math notation breaks plain text; terminal-first with browser fallback respects every install mode | Per equation in result | User reads math, not source | scidisplay/ + `/math` slash command |
| Tabular DataFrame view | Terminal + chat | 🟦 Designed | Tables are first-class data; Rich Table protocol exists | Per result containing tabular shape | User reads structured data clearly | `rich.table` consumer in scidisplay/ |
| External-render handoff (SVG/PNG → default viewer) | Terminal | 🔵 Strategic | Some plots can't be inlined; opening in viewer is the universal escape | When the inline form isn't enough | User gets full-resolution figures | scidisplay/ + OS `open` |

### UI patterns (the recurring shapes)

These are the **structural templates** affordances reuse. Adding a new
affordance to the catalog above usually means picking the matching
pattern below — not inventing a new shape. Coherence comes from this
discipline.

#### P1. Slash-command four-step flow

> **Status:** ✅ Shipped — `chat/commands.py`

Every slash command follows: **collect context → present choices →
confirm intent → dispatch + report**. The user always sees the
underlying `axi` invocation that's about to run. This is the concrete
embodiment of the *CLI ↔ Chat parity invariant* described in
[`§Chat ↔ CLI Resolution`](#chat--cli-resolution).

#### P2. Resolution preview

> **Status:** 🟦 Designed → Phase 2

```
> clean up old worktrees

  I'll run: axi hygiene list worktrees --prune
  → Approve [y/N]
```

Used at every chat→CLI boundary. Reused by error handler when a typed
verb has a close match.

#### P3. Three-part error

> **Status:** ✅ Shipped (T1.7)

```
Error: <one-line summary>

  × <root cause>
  │
  │ <context, why-this-matters>
  │
  help: <next step>
```

Reused by *did-you-mean*, by approval rejection, by validator failures
in REV-U.

#### P4. Severity-tagged finding (group → file → line)

> **Status:** ✅ Shipped (#108 REV-U, #109 TIDY)

```
[STALE] /Users/example/Projects/foo
         branch: feat/x  head: abc123def
         - S2: branch 'feat/x' has been deleted on origin
         - S3: HEAD is already an ancestor of origin/main
```

Used wherever a report mixes severities. Footer line follows P5.

#### P5. Footer summary

> **Status:** ✅ Shipped — multiple call sites

```
─── 1,234 in / 567 out · $0.02 this turn · $0.15 session ───
```

Pattern: `─── <metric A> · <metric B> · <metric C> ───` joined by
middle-dot. Used in chat cost line, REV-U footer (`N findings (Bb · Mm
· mn · n nits) across F files · ~Ts`), `axi hygiene status`, etc.

#### P6. Approval corner-frame

> **Status:** ✅ Shipped (T4.10)

Box-drawing characters (`┌`, `└`, `─`) form the visual frame for
approval prompts. Width-aware (wraps to terminal columns). No
triple-hyphen substitute — the box is the signal that the user must
stop and look.

#### P7. Tier-graduation hint

> **Status:** 🟦 Designed → Phase 1

```
↑ You've reached `core` for `tidy` — 4 new verbs are now suggested.
```

Single-line, leading `↑` glyph (the same glyph used by the
update-available banner — both are "elevation" notifications). Appears
at most once per graduation event.

#### P8. Hot-load summary

> **Status:** 🟦 Designed → Phase 2

```
$ axi ext install <name>
  ✓ installed in 2.1s
  ✓ N new verbs available now: <noun> {verb1, verb2, …}
  ✓ shims refreshed for: claude, vscode
```

Lead with `✓` per success line; sub-info indented. Same shape used by
`axi update` for the "shims refreshed" line.

### Encoded conventions

These are the **micro-conventions** that recur across patterns. They
are spec'd here so that adding a new affordance doesn't require
re-litigating each one.

#### Verb-naming: `<noun> stat <resource>` for observability

Reserved shape under any agent that has a hygiene or
observability mandate (TIDY canonical; TRIAGE rolling-health stats
follow the same shape). Reads as "stats about `<resource>`",
composes for multi-resource queries, and never collides with verbs
that read or write the underlying resource.

| Use this … | … not this | Why |
|---|---|---|
| `axi hygiene stat mem` | `axi hygiene memory` | "memory" reads as access; we already have `axi memory show` for actual memory contents |
| `axi hygiene stat disk` | `axi hygiene disk` | Same risk: positional `disk` says "I'm reporting *on* disk" |
| `axi hygiene stat net` | `axi hygiene net` | — |
| `axi hygiene stat mem disk net` | (no parallel) | One verb, multiple positionals — multi-resource report in one call |
| `axi triage stat health` | `axi triage health` | "health" reads as a check; "stat health" reads as "stats about health" |

**Why positional, not flag, not hyphenated-verb.** Considered three
shapes:

| Shape | Verdict |
|---|---|
| `axi hygiene stat-mem` (verb-with-resource) | Verb proliferation; one new verb per new resource; doesn't compose for multi-resource |
| `axi hygiene stat --mem` (single verb + flag) | Boolean flag is ambiguous (include? scope?); poor tab-discoverability of available resources |
| **`axi hygiene stat mem`** (single verb + positional) | One canonical verb, resources discoverable via positional tab-complete, multi-resource composes naturally, idiomatic (cf. `kubectl get pods`, `git remote add`) |

The positional form wins on all three axes: discoverability,
composition, and verb-economy.

**Composition with the tier model.** `axi hygiene stat <resource>`
defaults to `core` tier (operators see it). `axi hygiene stat <resource>
--raw` and `axi hygiene stat <resource> --json` default to `advanced`
(power-user firehose). `axi hygiene stat` with no resource shows a
one-line summary per known resource (the menu form), at `core`.

**Composition with the addressing grammar.** Federated stats reuse
the addressing convention: `axi hygiene stat mem --node @bens` reports on
mem stats from a peer node. (Phase 2 with the federated-addressing
work; not Phase 1.)

Adding a new observability surface to any hygiene-class agent uses
this shape; deviating requires documenting why here.

#### Glyphs

| Glyph | Meaning | Where used |
|---|---|---|
| `✓` | Success | Hot-load lines, install status, completion install |
| `×` | Hard failure | Error first column |
| `!` / `⚠️` | Warning, attention | Destructive confirm, restart-recommended |
| `↑` | Elevation / upgrade | Update-available banner, tier-graduation hint |
| `→` | Suggested next step | Help epilogue, escalation prompt |
| `│` | Continuation in error block | P3 error pattern, multi-line approval frame body |
| `—` | Field separator (em-dash) | Header lines |
| `·` | Inline separator (middle-dot) | Footer summaries (P5), inline metrics |

#### Severity color

| Severity | Color (terminal) | Used by |
|---|---|---|
| `blocker` | red | REV-U findings |
| `major` | yellow | REV-U findings, restart-recommended |
| `minor` | cyan | REV-U findings |
| `nit` | dim | REV-U findings, hygiene non-actionable items |

Color is **redundant**, not load-bearing — every severity has a text
label so colorblind users and `--no-color` mode lose nothing.

#### Box-drawing

Used for approval frames (P6), and for visual grouping in `axi tidy
status` table sections. Always width-aware; never hardcoded to a
column count.

#### Generated-file marker

```
<!-- generated by `axi commands generate` — do not edit -->
```

Every file emitted by the cross-harness shim generator carries this
marker on a line near the top so users and tooling can distinguish
generated from hand-authored shims. A future-equivalent marker for
auto-generated AEOS manifests / completion snapshots reuses the same
shape.

#### Provenance via= field

Every action recorded in the CLI history stream carries a `via=`
field (`cli`, `chat`, `remote-trigger`, etc.). UI affordances that
display history (e.g. session list, hand-off context) surface this
field so the user always knows *how* an action was issued.

### Composition rules

- **Pattern reuse over invention.** A new affordance picks an
  existing pattern unless none fits. Inventing a new shape requires
  documenting it here.
- **Tier governs presence, not power.** An affordance that surfaces
  via a tier filter (e.g. tier-graduation hint) hides itself for
  users who haven't earned it; the underlying function remains
  invocable directly. We never gate *capability* by visibility.
- **Cross-harness fidelity is best-effort.** Patterns P1–P8 are
  spec'd for terminal + chat; shim renderers reuse them where the
  harness allows (Claude shim's markdown body can render P3 error
  shape inline; Cursor's flat command list can't render the corner-
  frame and falls back to plain confirm). The generator records the
  fidelity gap on each emitted shim's frontmatter so users know what
  to expect.
- **Affordances share underlying functions.** The error formatter
  used in P3 is the same module the did-you-mean suggestion calls;
  the footer summary used in P5 is the same helper consumed by
  REV-U, TIDY status, and the chat cost line. This is the technical
  expression of the visual coherence above.

### Open questions

1. **Sci-display affordances.** The catalog above lists envisioned
   inline-plot, equation-render, table-view, and external-render
   affordances. The PRD for `scidisplay/` should formalize each
   against the five attributes (placement / dynamism / impact /
   function / status) — out of scope for this spec but blocked on
   that PRD landing.
2. **Cross-harness fidelity floor.** What's the minimum subset of
   patterns P1–P8 every shim renderer must support? Today we let
   each renderer degrade gracefully; we may want a published floor
   so users have predictable expectations across harnesses.
3. **Accessibility.** Color is currently redundant; box-drawing has
   no visual equivalent for screen readers. A pass on screen-reader
   compatibility (and on `--no-color --no-glyphs` plain-text mode)
   should produce a parallel patterns subsection.
4. **Internationalization.** All affordance copy above is English-
   only. A future i18n layer would parameterize message strings; the
   patterns themselves are language-agnostic.

---

## Configuration

### Config File Location

| Platform | Path |
|----------|------|
| macOS / Linux | `~/.axi/config.toml` or `~/.config/axi/config.toml` |
| Runtime | `runtime/config/` (per-project) |
| Example | `runtime/config.example/` (shipped defaults) |

### Key Config Files

| File | Purpose |
|------|---------|
| `config.toml` / `settings.toml` | General settings |
| `llm-providers.toml` | LLM provider configuration (endpoints, tiers, models) |
| `infrastructure.toml` | Service endpoints (Ceph, OpenBao, etc.) |
| `install.toml` | Environment-aware install steps |
| `retention.yaml` | Data retention policies |
| `logging.toml` | Log levels and sinks |

---

## Extension System

Extensions register CLI commands, tools, connections, and agents via `axiom-extension.toml`:

```toml
[extension]
name = "signals"
version = "0.6.2"
description = "Signal ingestion and analysis agent"

[[cli.commands]]
noun = "signal"
module = "axiom.extensions.builtins.signals.cli"
description = "Agentic signal ingestion pipeline"

[[connections]]
name = "outlook"
type = "oauth2"
description = "Microsoft 365 for voice memo ingestion"
```

### Discovery Tiers

1. **Builtin** — `src/axiom/extensions/builtins/` (shipped with package)
2. **Installed** — PyPI packages with `axiom-extension.toml` in package root
3. **User** — `~/.axi/extensions/` (local user extensions)

---

## Branding

The CLI supports domain-specific branding via `axiom.infra.branding`. The same codebase powers:

| Product | Package | CLI Name | Banner |
|---------|---------|----------|--------|
| **Axiom** (platform) | `axi-platform` | `axi` | Generic intelligence platform |
| A domain consumer (e.g. an engineering assistant) | `domain-os` | `dom` | Domain engineering assistant |

Domain products inherit all Axiom commands and add domain-specific extensions.

---

## Shell Completions

Auto-completion is **load-bearing for incremental revelation**: it is the
mechanism by which a typed `<Tab>` reveals exactly the verbs the user has
been graduated into. Completion scripts must work in **every supported
install mode and shell** with no manual configuration in the happy path.

### Supported matrix

| Shell | Install modes covered |
|---|---|
| `bash` | pip-venv, Homebrew, system, Docker, frozen binary |
| `zsh` | (same) |
| `fish` | (same) |
| `PowerShell` | pip-venv, Homebrew (Windows), MSI installer |

### Lifecycle (`axi completions`)

```bash
axi completions install [--shell bash|zsh|fish|powershell] [--scope user|system]
axi completions print   --shell bash       # emit script to stdout for piping
axi completions refresh                    # invoked by `axi update`
axi completions uninstall
```

- **First-run install**: the first `axi` invocation after a fresh install
  detects the user's shell, prompts once for consent, and either drops
  the completion script in the standard location for that shell + scope
  (e.g. `~/.bash_completion.d/axi` on Linux, `~/.zsh/completions/_axi`
  on macOS) or prints the one-liner if the user declines.
- **Refresh on update**: `axi update` (and the `axi commands generate`
  flow that runs after it) refreshes completions so newly-installed
  extensions' verbs become tab-discoverable. This is the same hook the
  cross-harness shim regenerator uses.
- **Frozen-binary fallback**: when no Python interpreter is available at
  completion time (frozen binary, shell sourced before `axi`'s venv is
  active), completion uses a static snapshot generated at install time.
  The snapshot is refreshed on `axi update`.

### Completion content respects the surfacing rule

Completions consult the user's `~/.axi/competency.json` and surface
**only verbs reachable under the effective surfacing rule** (see
*Capability Tiers* above). The user can always type the full noun-verb
explicitly to invoke a verb above their current tier — completion gates
*automatic suggestion*, never invocation.

### Implementation backbone

`argcomplete` remains the in-process completion library, but the wrapper
script is now generated by `axi completions print` so it can:

- Inject the tier filter via an env var that argcomplete's hook reads
- Snapshot the completion definition for frozen-binary mode
- Produce shell-native syntax (zsh `_arguments`, fish `complete`,
  PowerShell `Register-ArgumentCompleter`) where argcomplete's bash
  wrapper isn't a clean fit

```bash
# Manual, if first-run install was declined:
eval "$(axi completions print --shell bash)"     # bash
eval "$(axi completions print --shell zsh)"      # zsh
axi completions print --shell fish | source      # fish
axi completions print --shell powershell | iex   # powershell
```

---

## Distribution

| Method | Command |
|--------|---------|
| **PyPI** | `pip install axi-platform` |
| **PyPI (with domain)** | `pip install example-consumer` (includes axi-platform) |
| **Development** | `pip install -e ".[all]"` |

---

## Testing Strategy

| Test Type | Coverage |
|-----------|----------|
| Unit tests | Command parsing, tool definitions, usage tracking |
| Integration tests | Extension discovery, CLI dispatch, session store |
| E2E tests | `axi chat` → tool use → response pipeline |
| Smoke tests | `axi --help`, `axi status`, extension loading |

Current test count: **2270+** (Axiom platform)

---

## Performance

| Metric | Current |
|--------|---------|
| `axi --help` | <200ms |
| Extension discovery | <100ms (cached) |
| Chat first response | Depends on LLM provider (1-5s typical) |
| RAG search | <500ms (pgvector) |

---

## Unimplemented / Planned

These nouns appear in specs or PRDs but have no CLI implementation:

| Noun | Status | Notes |
|------|--------|-------|
| `data` | Not implemented | Data platform (Iceberg/dbt) not wired to CLI |
| `repo` | Extension exists, no CLI | `axiom-extension.toml` defined but zero `[[cli.commands]]` |
| `demo` | Not implemented | Guided walkthrough deferred |
| `graph` | Spec'd ([spec-knowledge-graph.md](spec-knowledge-graph.md)) | Apache AGE, Phase 0.1 |
| `dmp` | Spec'd ([prd-doe-data-management.md](../requirements/prd-doe-data-management.md)) | FAIR/DMP framework, P4 |

---

## Related Documents

- [CLI PRD](../requirements/prd-axi-cli.md) — Product requirements
- [Agent Architecture](spec-agent-architecture.md) — Agent capabilities and delegation
- [Session Store](spec-session-store.md) — PostgreSQL session backend
- [Gateway & Routing](spec-model-routing.md) — LLM provider routing
- [RAG Architecture](spec-rag-architecture.md) — Knowledge retrieval
- [Observability](spec-observability.md) — Metrics and logging
- [Federation](spec-federation.md) — Multi-node protocol
- [Canary Nodes](spec-canary-nodes.md) — Release promotion via `axi release`
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
