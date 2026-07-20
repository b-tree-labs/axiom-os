# Built-in Root MCP Server — Technical Specification

**Status:** Draft  •  **Owner:** Benjamin Booth  •  **Last updated:** 2026-05-01
**Companion ADR:** [`adr-038-builtin-mcp-server.md`](../adrs/adr-038-builtin-mcp-server.md)
**Companion PRD:** [`prd-builtin-mcp-server.md`](../prds/prd-builtin-mcp-server.md)

---

## 1. Goals

- Specify the runtime contract of Axiom's single root MCP server: transport, lifecycle, aggregation algorithm, tool/resource/prompt naming, error semantics, and back-compat with `extensions/mcp_generation.py`.
- Specify the AEOS manifest extension `[extension.mcp]` precisely enough that `axi ext lint` can validate it deterministically.
- Specify the M-O drift-detection check + RACI integration so the runtime adaptation path is implementable without further design.
- Specify the per-harness adapter recipe template so all 17 docs follow one shape.
- Stay aligned with AEOS 0.1 §4 capability kinds, §5 layout, §6 manifest, §7 entry points; never break those contracts.

## 2. Out of v1 (queued, not punted)

- **Remote HTTP/SSE auth.** Stub the transport in v1 (so the code path exists and tests cover negative auth) but ship token + principal modes in Phase 5 per ADR-038 D6. The `auth = "principal"` mode requires the federation principal-binding flow to be solid for non-Axiom clients.
- **Federation-mediated tools.** A remote MCP client whose tool call federates to a different Axiom node is Phase 6 work; the trust-graph semantics require their own ADR.
- **Streaming tool responses.** The MCP `progress`/`stream` mechanism is supported by the SDK but not surfaced in v1; tools return one whole response. Streaming lands when there's a real consumer.
- **Surface versioning + deprecation policy.** A `mcp_surface_version` field per tool + a deprecation-window policy is queued as a follow-up after Phase 1 lands.
- **Multi-modal in tool args/responses.** The SDK supports `ImageContent`, `AudioContent`. v1 returns `TextContent` only; multi-modal lifts when chat-multimodal lands in the parallel session.

## 3. Architecture

```
                    ┌──────────────────────────────────────────────────────────┐
                    │                  Peer harness                              │
                    │        (Claude Code / Cursor / Goose / LangChain / …)      │
                    └─────────────────────┬─────────────────┬──────────────────┘
                          stdio (default) │                 │ HTTP/SSE (flagged, Phase 5)
                                          ▼                 ▼
                    ┌───────────────────────────────────────────────────────────┐
                    │ axiom.extensions.builtins.mcp.server (this spec)            │
                    │   • mcp.server.Server instance                               │
                    │   • list_tools / call_tool / list_resources / read_resource   │
                    │   • list_prompts / get_prompt                                 │
                    │   • routes via dispatch table built by AggregationRegistry    │
                    └─────────────────────┬─────────────────────────────────────┘
                                          │
                                          ▼
                    ┌───────────────────────────────────────────────────────────┐
                    │ axiom.extensions.builtins.mcp.aggregation.AggregationRegistry │
                    │   • walks discovery.list_extensions()                          │
                    │   • parses each [extension.mcp] block                          │
                    │   • merges with PlatformPrimitives                             │
                    │   • produces deterministic MCPSurface (content-hashed)         │
                    │   • caches at ~/.axiom/mcp/surface.json                        │
                    └───┬───────────────────────────────────────────────────────┘
                        │                                          │
                        ▼                                          ▼
       ┌──────────────────────────────────┐     ┌─────────────────────────────────────┐
       │ Platform primitives (always on)   │     │ Per-extension contributions          │
       │ axiom_memory__*  (memory)          │     │ Read from each extension's manifest │
       │ axiom_federation__* (federation)   │     │ via [extension.mcp] + defaults rule │
       │ axiom_rag__retrieve (rag)           │     │ Module imports lazy (on call_tool)  │
       │ axiom_signals__brief (signals)      │     │                                       │
       │ axiom_node__status (diagnostics)    │     │                                       │
       └──────────────────────────────────┘     └─────────────────────────────────────┘

       ┌────────────────────────────────────────────────────────────────────────┐
       │ Drift detection + auto-regen                                              │
       │ hygiene/M-O._check_mcp_surface_drift() — runs on every heartbeat           │
       │   • re-walks manifests, computes fresh content hash                        │
       │   • compares to cached surface.json hash                                   │
       │   • on divergence persisting ≥ 2 heartbeats: propose regen via RACI        │
       │ extension.post_install hook subscriber — immediate refresh on install/      │
       │   uninstall/update events                                                  │
       └────────────────────────────────────────────────────────────────────────┘
```

## 4. Module layout

```
src/axiom/extensions/builtins/mcp/                    # AEOS flat layout (builtin = true)
├── __init__.py                                        # public symbols re-exported
├── axiom-extension.toml                               # AEOS manifest
├── server.py                                          # `python -m … .server` entry; stdio loop
├── http_server.py                                     # HTTP/SSE transport (stubs in v1)
├── aggregation.py                                     # AggregationRegistry + MCPSurface
├── platform_primitives.py                             # the 7 always-on tools + 2 resources
├── manifest_schema.py                                 # parses [extension.mcp] block
├── cli.py                                             # `axi mcp` noun (serve/status/regen/list-tools/inspect/clients)
├── client_writers.py                                  # Tier-1 harness config writers (subsumes mcp_generation.py)
├── drift.py                                           # M-O._check_mcp_surface_drift() helper
├── subscriber.py                                      # extension.post_install / post_uninstall hook handler
├── agents/                                            # (none in v1; reserved if MCP-specific agent emerges)
├── tools/                                             # (none in v1; tools live in platform_primitives.py + extensions)
├── tests/
│   ├── unit_tests/
│   │   ├── test_standard.py                           # ExtensionStandardTests inheritance
│   │   ├── test_aggregation.py
│   │   ├── test_platform_primitives.py
│   │   ├── test_manifest_schema.py
│   │   ├── test_server_lifecycle.py
│   │   ├── test_drift.py
│   │   └── test_cli.py
│   ├── integration_tests/
│   │   ├── test_stdio_roundtrip.py                    # client → server smoke
│   │   ├── test_three_extensions.py                   # memory + signals + hygiene end-to-end
│   │   └── test_extension_install_refresh.py          # extension.post_install → surface refresh
│   └── fixtures/
│       └── manifests/
│           ├── opt_in_minimal.toml
│           ├── opt_out_explicit.toml
│           ├── per_capability_overrides.toml
│           └── invalid_collisions.toml
└── docs/
    ├── prd.md                                          # extension-level PRD (links to docs/prds/prd-builtin-mcp-server.md)
    ├── spec.md                                         # extension-level spec (links to this doc)
    └── decisions/
        └── (extension-level ADRs as they emerge)
```

## 5. Server lifecycle

### 5.1 stdio entry point

```python
# axiom.extensions.builtins.mcp.server
async def run() -> None:
    """Serve MCP over stdio. Entry point for `python -m axiom.extensions.builtins.mcp.server`."""
    surface = AggregationRegistry.from_node().build()
    server = build_server(surface)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def build_server(surface: MCPSurface) -> Server:
    server: Server = Server("axiom-root")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return surface.tools  # already MCP Tool objects

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        handler = surface.dispatch.get(name)
        if handler is None:
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
        try:
            result = await handler(arguments or {})
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    @server.list_resources()
    async def _list_resources() -> list[Resource]:
        return surface.resources

    @server.read_resource()
    async def _read_resource(uri: str) -> str:
        return await surface.resource_read(uri)

    @server.list_prompts()
    async def _list_prompts() -> list[Prompt]:
        return surface.prompts

    @server.get_prompt()
    async def _get_prompt(name: str, arguments: dict[str, str]) -> GetPromptResult:
        return await surface.prompt_get(name, arguments or {})

    return server
```

### 5.2 HTTP/SSE entry point (Phase 5; v1 stubs)

```python
# axiom.extensions.builtins.mcp.http_server
def serve_http(host: str, port: int, auth: AuthMode) -> None:
    if auth == AuthMode.LOCAL_STDIO:
        raise ValueError("local_stdio auth refuses HTTP transport — use stdio")
    if auth == AuthMode.PRINCIPAL:
        raise NotImplementedError("principal auth: Phase 5")
    # auth == AuthMode.TOKEN: stub raises in v1; Phase 5 wires bearer-token middleware
    raise NotImplementedError("HTTP transport: Phase 5")
```

The HTTP path exists in v1 only as a hard-fail-with-clear-message — so an operator who tries `axi mcp serve --http` gets a deterministic "Phase 5" message rather than an obscure import error.

### 5.3 Process model

- One subprocess per peer harness (the harness spawns `python -m axiom.extensions.builtins.mcp.server` via its config).
- Each subprocess loads the surface once at startup; surface is immutable for the lifetime of the process.
- A drift event regenerates the *cached* surface but does not affect already-running subprocesses; they pick up changes on next spawn.
- HTTP mode (Phase 5) is a single long-lived process that re-reads the cached surface on every request (cheap; ≤ 5ms).

## 6. AggregationRegistry

### 6.1 Algorithm

```
1. extensions = discovery.list_extensions()  # deterministic order from existing API
2. surface_inputs = []
3. surface_inputs.append(PlatformPrimitives.contributions())  # always first; cannot be shadowed
4. for ext in extensions sorted by ext.name:
       mcp_block = parse_mcp_block(ext.manifest_path)
       if mcp_block is None and not ext.has_explicit_mcp_optout:
           # lint flagged this earlier; runtime skips silently
           continue
       if mcp_block.enabled is False:
           continue
       contributions = resolve_contributions(ext, mcp_block)
       surface_inputs.append(contributions)
5. surface = MCPSurface.merge(surface_inputs)  # platform always wins; ext-vs-ext: first-load wins + warning
6. surface.content_hash = sha256(stable_json(surface.tools + surface.resources + surface.prompts))
7. write_cache(~/.axiom/mcp/surface.json, surface)
8. return surface
```

### 6.2 MCPSurface dataclass

```python
@dataclass(frozen=True)
class MCPSurface:
    tools: list[Tool]
    resources: list[Resource]
    prompts: list[Prompt]
    dispatch: dict[str, Callable[[dict], Awaitable[Any]]]   # tool name → handler
    resource_readers: dict[str, Callable[[str], Awaitable[str]]]   # uri prefix → reader
    prompt_getters: dict[str, Callable[[dict], Awaitable[GetPromptResult]]]
    content_hash: str
    generated_at: datetime
    sources: list[ContributionSource]    # provenance: which extension contributed which entries

    async def resource_read(self, uri: str) -> str: ...
    async def prompt_get(self, name: str, args: dict[str, str]) -> GetPromptResult: ...

    @classmethod
    def merge(cls, contributions: list[ExtensionContribution]) -> "MCPSurface": ...
```

### 6.3 Lint rule (enforced by `axi ext lint`)

Every AEOS extension MUST satisfy one of:

1. Has a `[extension.mcp]` block (with `enabled = true` or `enabled = false`).
2. Has a single-line manifest comment immediately above `[extension]` of the form `# mcp: not-applicable — <reason>` (where `<reason>` is non-empty free text).

A manifest with neither fails lint with:

```
ERROR: extension '<name>' has no [extension.mcp] block and no `# mcp: not-applicable` annotation.
       Add one of:
         [extension.mcp]
         enabled = true       # or false to opt out
       OR add a one-line comment explaining why MCP exposure does not apply:
         # mcp: not-applicable — pure CLI utility; nothing to expose
```

### 6.4 Collision rules

| Collision | Resolution |
|---|---|
| Platform tool name == extension tool name | Platform wins; extension entry is dropped with a lint warning. Extension authors must rename. |
| Two extensions declare the same MCP tool name | Lexicographic-first extension wins (deterministic via `sorted(extensions, key=name)`); other entry dropped with lint warning. |
| `[[extension.mcp.tool]]` references a non-existent `[[extension.provides]]` name | Lint error; extension fails validation. |
| `mcp_name` override collides with another extension's default | First-load wins; both entries warned at lint. |

### 6.5 Default tool-name pattern

`<prefix>__<capability_name>` where `<prefix>` defaults to `axiom_<extension.name>` (sanitised: lowercase, `_` for non-alnum). The double-underscore separator is deliberate — single underscore would collide with multi-word capability names.

Examples:
- Platform: `axiom_memory__compose`, `axiom_federation__list_peers`
- `memory` extension exposing tool `recall`: `axiom_memory_ext__recall`
- `signals` extension exposing tool `brief`: `axiom_signals_ext__brief`
- `signals` extension with `prefix = "signals"` override: `signals__brief`

(Note: `memory`-the-extension and `memory`-the-platform-module differ; the platform tools take the `axiom_memory__*` prefix unmodified, and the extension's prefix defaults to `axiom_memory_ext` to avoid collision. This is enforced by the lint rule.)

## 7. Manifest schema reference

### 7.1 `[extension.mcp]` (top-level block)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `true` if block present | Master switch. `false` opts out explicitly. |
| `prefix` | string | `axiom_<extension.name>` | Tool-name prefix; sanitised. |
| `visibility` | enum: `public`, `internal` | `public` | `internal` hides from `list_tools` unless client requests internal scope. |
| `auth` | enum: `local_stdio`, `token`, `principal` | `local_stdio` | Per-extension default auth requirement. |
| `description` | string | none | Human-readable; used in MCP server-info. |

### 7.2 `[[extension.mcp.tool]]` (zero or more)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Must match an existing `[[extension.provides]] kind = "tool"` entry's name. |
| `mcp_name` | string | `<prefix>__<name>` | Override the default MCP tool name. |
| `description_override` | string | from `provides.description` | Override the description shown to MCP clients. |
| `input_schema_module` | string | inferred | Dotted path to a JSON Schema (or pydantic model) that overrides the tool's own. |
| `hidden` | bool | `false` | Loaded but not advertised in `list_tools`; callable if name is known. |
| `allowed_principals` | list[string] | `["@*:local"]` | Matrix-style principal patterns. |
| `side_effects` | enum: `none`, `writes`, `external` | from `provides.side_effects` | Surfaced as MCP tool annotation. |

### 7.3 `[[extension.mcp.resource]]` (zero or more)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Identifier; surfaced in `list_resources`. |
| `uri_template` | string | required | Per RFC 6570; e.g., `axiom://compositions/{principal}/{cohort}`. |
| `mime_type` | string | `application/json` | MIME type of the response body. |
| `entry` | string | required | `module.path:funcname` returning a string body for a given URI. |
| `allowed_principals` | list[string] | `["@*:local"]` | Same as tools. |

### 7.4 `[[extension.mcp.prompt]]` (zero or more)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Identifier; surfaced in `list_prompts`. |
| `description` | string | required | Shown to MCP clients. |
| `arguments` | list[string] | `[]` | Argument names; described as required. |
| `entry` | string | required | `module.path:funcname` returning a `GetPromptResult` for given args. |

### 7.5 `[[extension.mcp.cmd]]` (zero or more)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `noun` | string | required | Must match an existing `[[extension.provides]] kind = "cmd"` noun. |
| `subcommands` | list[string] | all | Whitelist of subcommands to expose; absence = all. |
| `mcp_name_pattern` | string | `<prefix>__cmd_{verb}` | Pattern for the resulting tool names. |
| `allowed_principals` | list[string] | `["@<owner>:local"]` | Defaults stricter than tools — CLI commands often have side effects. |

CLI commands surfaced through this block become MCP tools whose handler shells out to `axi <noun> <verb> <args>` and captures stdout. The `args` argument is a typed dict per the verb's argparse layout.

### 7.6 Invalid configurations (lint errors)

- `name` references a non-existent `provides` entry.
- `mcp_name` collides with a platform tool name.
- `prefix` contains characters outside `[a-z0-9_]`.
- `allowed_principals` contains a malformed Matrix-style identity.
- `auth = "principal"` declared without any `allowed_principals` entries.
- `[[extension.mcp.cmd]]` with empty `subcommands = []` (use omission for "all").

## 8. Platform primitives surface

Always-on, contributed before any extension. Defined in `platform_primitives.py`.

| Tool | Source module | Acts as |
|---|---|---|
| `axiom_memory__compose` | `axiom.memory.composition.CompositionService` | Canonical write path for memory fragments. Args: `kind`, `content`, `provenance` (T,U,A,R). |
| `axiom_memory__retrieve` | `axiom.memory.composition.CompositionService` | Read fragments by filter. Args: `kind?`, `principal?`, `cohort?`, `limit?`. |
| `axiom_federation__list_peers` | `axiom.federation.cohort_registry` | Enumerate peers + trust state. Args: `cohort?`. |
| `axiom_federation__send` | `axiom.federation.transport` | Send signed federation message. Args: `target_principal`, `cohort`, `payload`. |
| `axiom_rag__retrieve` | `axiom.rag.retrieval` | Hybrid retrieval (vector + graph). Args: `query`, `k?`, `mode?`. |
| `axiom_signals__brief` | `axiom.infra.signals` | Run signal-briefing pipeline; returns latest brief. Args: `since?`, `kinds?`. |
| `axiom_node__status` | `axiom.diagnostics` | Node health: surface, slot, agent liveness. No args. |

| Resource | URI template | Source |
|---|---|---|
| Principal card | `axiom://principals/{principal}` | `axiom.identity` — public identity card |
| Node info | `axiom://node/info` | platform — node identity, surface, slot, version |

All platform tools default to `allowed_principals = ["@<owner>:local"]` — only the node-owning principal may call. Per `feedback_axiom_domain_agnostic`, none of these tools' descriptions reference any domain.

## 9. CLI surface (`axi mcp`)

| Command | Behaviour |
|---|---|
| `axi mcp serve` | Run the stdio server in foreground. Equivalent to `python -m axiom.extensions.builtins.mcp.server`. |
| `axi mcp serve --http --port <n> --auth <mode>` | Run HTTP/SSE; v1 stubs raise NotImplementedError unless `auth=local_stdio` (which is rejected for HTTP), guarding the Phase-5 work. |
| `axi mcp status` | Print cached surface summary: tool count, resource count, prompt count, content hash, generated_at, sources. |
| `axi mcp regenerate` | Force re-walk extensions; rewrite `~/.axiom/mcp/surface.json`; print diff vs. previous. Idempotent. |
| `axi mcp list-tools` | Pretty-print the current tool list (name, description, source extension). |
| `axi mcp inspect <tool>` | Show tool's input schema, source extension, allowed principals, side-effect category. |
| `axi mcp clients [--write] [--harness <name>]` | List supported peer harnesses with recipe links; `--write` writes the config block(s) for one or all Tier-1 harnesses. Replaces `axi mcp generate`. |
| `axi mcp generate` | Deprecated alias for `axi mcp clients --write`. Prints a one-line deprecation notice on first use per session. |
| `axi mcp tokens add/list/revoke` | Phase-5; v1 stubs print "Phase 5 — see ADR-038 D6". |

## 10. Drift detection + RACI integration

### 10.1 M-O check

```python
# axiom.extensions.builtins.mcp.drift  (also imported by hygiene/node_health.py)
def check_mcp_surface_drift(node_root: Path) -> DriftFinding | None:
    """Return a DriftFinding if cached surface no longer matches manifests; None if clean.

    Called by hygiene/M-O on every heartbeat. Debounce (caller-side) requires the
    finding to persist across 2 consecutive heartbeats before proposing regen.
    """
    cached = load_cached_surface(node_root)
    fresh_hash = compute_fresh_content_hash(node_root)
    if cached is None or cached.content_hash != fresh_hash:
        return DriftFinding(
            cached_hash=cached.content_hash if cached else None,
            fresh_hash=fresh_hash,
            kind="mcp.surface.stale",
            severity="info",
            proposal=Proposal(
                action="axi mcp regenerate",
                description="MCP surface cache is stale; regen recommended",
                preapproval_pattern="mcp.surface.regen.*",
            ),
        )
    return None
```

### 10.2 Hygiene integration

`hygiene/node_health.py` adds the check to its existing heartbeat sweep:

```python
def collect_health_findings() -> list[Finding]:
    findings = []
    findings.extend(_check_local_drift())               # existing
    finding = check_mcp_surface_drift(node_root())      # new
    if finding:
        findings.append(finding)
    return findings
```

### 10.3 RACI proposal flow

The `Proposal.preapproval_pattern = "mcp.surface.regen.*"` integrates with the existing RACI store at `~/.axiom/raci/preapprovals.json`. On first occurrence:

```
M-O: I detected MCP surface drift (3 tools added since last regen).
     Propose: run `axi mcp regenerate` to refresh the cache.
     [a]ccept once | [A]ccept always for `mcp.surface.regen.*` | [d]eny | [s]chedule for later
```

Pattern-accepted: subsequent regens are silent execution (M-O calls `axi mcp regenerate` and logs to its own activity log). Per `feedback_raci_automation_escalation`'s "3 nos = stop asking" rule, denying three times in a row makes M-O stop proposing for 24h.

### 10.4 extension.post_install subscriber

In addition to M-O's heartbeat-based detection, the mcp built-in subscribes to immediate-fire events:

```toml
[[extension.provides]]
kind = "hook"
events = ["extension.post_install", "extension.post_uninstall", "extension.post_update"]
entry = "axiom.extensions.builtins.mcp.subscriber:on_extension_changed"
fail_mode = "warn"
description = "Refresh MCP surface cache when the extension set changes."
```

The subscriber regenerates the surface immediately (no debounce required — the install itself is the explicit user action).

## 11. Adapter recipe template

Every doc at `docs/working/mcp-harness-adapters/<harness>.md` follows this shape:

```
# <Harness Name> — MCP adapter recipe for Axiom

Last tested against: <harness version>  •  Tested on: <date>
Tier: <1 / 2 / 3>  •  Transport: <stdio / HTTP-SSE>

## 1. Install

`<one or two commands to install the harness, with platform notes>`

## 2. Connect to Axiom

### Where the config goes
`<path to the harness's MCP config file, with platform variants>`

### Block to add
```json/yaml/toml
<the exact config block, with `${AXIOM_PYTHON}` etc. placeholders explained>
```

(or, if the harness supports it: `axi mcp clients --harness <name> --write` does this for you.)

## 3. Verify

`<commands to run inside the harness — e.g., "/mcp" in Claude Code or "list-tools" in Goose>`

Expected output: `axiom_memory__compose`, `axiom_federation__list_peers`, … (≥ 7 platform tools).

## 4. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "command not found" | venv not on PATH | `<harness-specific command-path setting>` |
| Surface mismatch | extension installed after harness start | Restart the harness OR run `axi mcp regenerate` |
| Auth denied (HTTP only) | token missing / revoked | `axi mcp tokens add <client>` |

## 5. Notes

`<any harness-specific gotchas — e.g., Cline needs absolute python path; Goose has its own permissions UI>`
```

The 17 recipes are listed in PRD §5 / ADR-038 D7. The index README enumerates them and shows last-tested-against version per row.

## 12. Test surface

### 12.1 Standard tests (axiom-tests inheritance)

```python
class TestMCPExtension(ExtensionStandardTests):
    @pytest.fixture
    def extension_manifest_path(self):
        return Path(__file__).parents[2] / "axiom-extension.toml"
```

### 12.2 Aggregation tests

- `test_aggregation_idempotent`: two consecutive builds against the same extension set produce identical content hashes.
- `test_aggregation_platform_first`: PlatformPrimitives entries always appear before extension entries; an extension cannot shadow `axiom_memory__compose`.
- `test_aggregation_collision_resolution`: two extensions declaring the same `mcp_name` resolve lexicographic-first; a `Warning` is captured.
- `test_aggregation_opt_out_explicit`: `enabled = false` excludes contributions even if `[[extension.mcp.tool]]` blocks are present.
- `test_aggregation_skips_disabled`: `ext.enabled = False` (extension-level disable) excludes the extension entirely.

### 12.3 Server lifecycle tests

- `test_stdio_handshake`: an `mcp` SDK client over an in-memory pipe completes initialization and `list_tools` against a built surface.
- `test_call_tool_dispatch`: `call_tool("axiom_node__status")` routes to the platform handler and returns valid JSON.
- `test_call_tool_unknown`: `call_tool("does_not_exist")` returns a structured error, not a crash.
- `test_call_tool_exception_translated`: a handler that raises returns `{"error": "<Type>: <msg>"}`.

### 12.4 Manifest schema tests

- `test_parse_minimal`: `[extension.mcp]` with only `enabled = true` parses to a default schema.
- `test_parse_overrides`: per-tool `mcp_name`, `description_override`, `allowed_principals` apply.
- `test_lint_missing_block_and_no_comment`: lint fails.
- `test_lint_explicit_optout`: lint passes for both `enabled = false` and `# mcp: not-applicable — …`.
- `test_lint_invalid_principal_pattern`: lint fails with clear message.
- `test_lint_collision_with_platform`: lint warns and drops the conflicting entry.

### 12.5 Drift tests

- `test_drift_detected_on_manifest_change`: write a fresh manifest, expect `check_mcp_surface_drift()` to return a finding.
- `test_drift_clean_when_synced`: post-regen, finding is `None`.
- `test_drift_debounce_two_heartbeats`: simulated heartbeats; proposal fires only after the second consecutive divergence.

### 12.6 Integration tests (no DB mocks; use real SQLite per project rule)

- `test_three_extensions`: enable `memory` + `signals` + `hygiene` MCP blocks; verify all three surfaces appear in `list_tools` with correct prefixes.
- `test_extension_install_refresh`: invoke `extension.post_install` event; verify surface cache rewritten and content hash updated.
- `test_axi_mcp_clients_writes_claude_code_config`: temp dir; run command; verify `.mcp.json` written with `axiom-root` entry.

### 12.7 No-DB-mock rule compliance

Per project rule (no DB mocking in tests): all tests touching CompositionService use `tmp_axiom_home` fixture from `axiom-tests` to get a fresh on-disk SQLite per test. Any "mock memory" pattern is an automatic test-review failure.

## 13. Back-compat with `mcp_generation.py`

| Today | Phase 1 | Phase 3 | Phase 6 (post Prague +6mo) |
|---|---|---|---|
| `axi mcp generate` writes `.mcp.json` from per-extension `[mcp_servers.*]` blocks | Same — module untouched | `axi mcp generate` becomes alias for `axi mcp clients --write`; deprecation notice once per session | Module deleted; alias removed |
| `mcp_generation.MCPTarget` enum used in user code | Same | Imports re-exported from `mcp.client_writers` | Deleted |
| Extensions declaring `[mcp_servers.<name>]` | Same — still emit per-extension server entries | Bridge: a `[mcp_servers.*]` block without a `[extension.mcp]` block emits a one-line `axi ext lint` warning recommending migration | Lint error |

Nothing in the public CLI breaks during Phase 1–3.

## 14. Logging + observability

- Server lifecycle events go to `~/.axiom/logs/mcp/server.log` per the existing logging-extension conventions.
- Every `call_tool` produces an `EventBus` event `mcp.tool.invoked` with `{tool_name, principal, latency_ms, error?}`. Existing observers (cost meter, audit log) auto-pick up.
- Surface regen produces `mcp.surface.regenerated` with `{old_hash, new_hash, source_count, generated_at}`.
- M-O drift findings produce `mcp.surface.drift_detected` with `{cached_hash, fresh_hash, debounce_count}`.

All four event names follow the `<scope>.<verb>` naming from `spec-hooks.md` §4.

## 15. Open items

- (Phase 2) Whether to add a v1 `[[extension.mcp.agent]]` block surfacing agents as tools (their `invoke()` becomes a tool call). ADR-038's open question; resolved before spec freeze.
- (Phase 4) Pre-approval RACI persistence path — `~/.axiom/raci/preapprovals.json` (RIVET precedent) or per-built-in. Resolved by Phase 4 start.
- (Phase 5) Token storage at rest — plaintext JSON, OS keychain, both. Resolved by Phase 5 spec.
- (Phase 6) Surface versioning + deprecation policy. Tracked as a follow-up after Phase 1 lands.
- Federation-mediated MCP calls (a remote client's `call_tool` federates to a different node) — out of scope; future ADR.

---

_Copyright (c) 2026 B-Tree Ventures, LLC. Apache-2.0 licensed._
