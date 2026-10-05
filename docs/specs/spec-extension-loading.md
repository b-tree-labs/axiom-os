# Spec: Extension Loading — Discovery, Hot-Swap, and the WASM Migration Target

**Status:** 🟦 Designed — Hot-load v1 (Python loader) → Phase 2; WASM
loader → Phase 3 (Strategic). See
[`prd-axi-cli.md §Status & Phasing`](../prds/prd-axi-cli.md#status--phasing).
**Owner:** Benjamin Booth •  **Last updated:** 2026-05-02
**Parents:** [prd-axi-cli.md](../prds/prd-axi-cli.md), [spec-aeos-0.1.md](spec-aeos-0.1.md)
**Related:** [spec-axi-cli.md](spec-axi-cli.md), [prd-commands-generator.md](../prds/prd-commands-generator.md) — the generator's auto-refresh subscribes to the `extensions.changed` event this spec defines

---

## 1. Scope

This spec governs how Axiom **discovers, loads, hot-reloads, and
sandboxes** extensions across the lifetime of an `axi` process and the
daemons it spawns. It is the source of truth for:

- Extension discovery order, caching, and invalidation.
- The `axi ext install / upgrade / uninstall` lifecycle and the events
  it publishes.
- Hot-swap protocol — including what *cannot* be hot-swapped safely and
  must fall back to a restart prompt.
- The forward-looking WASM Component Model migration target, framed so
  that the v1 Python-based loader and the v2 WASM-based loader can
  coexist behind a single manifest field.

Other surfaces consume this spec but do not duplicate its rules:

- `spec-axi-cli.md §Capability Tiers` consumes the discovery surface to
  compute the effective surfacing rule.
- `prd-commands-generator.md` consumes the discovery surface to emit
  per-harness shims; the generator is a downstream listener of the
  hot-load events specified here.
- `spec-aeos-0.1.md` defines the manifest schema; this spec governs
  *runtime* behavior, not declarative shape.

---

## 2. Discovery (v1, Python loader)

### 2.1 Sources, in precedence order

1. **Core commands** — hard-coded SUBCOMMANDS in `axiom_cli.py`.
2. **Builtin extensions** — `src/axiom/extensions/builtins/*/axiom-extension.toml`.
3. **PyPI-installed extensions** — site-packages packages that ship an
   `axiom-extension.toml` at their package root.
4. **User extensions** — `~/.axi/extensions/*/axiom-extension.toml`.
5. **Project extensions** — `<cwd>/.axi/extensions/*/axiom-extension.toml`.

Higher-numbered sources shadow lower for the same noun (consistent with
the `builtin < user < project` tier hierarchy used throughout the platform).

### 2.2 Cache and TTL

The discovery cache lives in the `ExtensionRegistry` singleton (see §4)
and is invalidated by:

- An `extensions.changed` event (see §4.2).
- A manifest mtime change (when the optional filesystem watcher is
  enabled — see §5.4).
- Explicit `registry.invalidate()` callers (test fixtures; `axi ext`
  verbs).

There is **no time-based TTL.** Cache invalidation is event-driven so
that long-running daemons don't pay a discovery cost on every heartbeat.

### 2.3 Manifest parsing

Each `axiom-extension.toml` is parsed once per cache lifetime. The
parser is forgiving for unknown top-level keys (per AEOS additivity
posture) but strict for capability-kind blocks: an invalid `kind=cmd`
block fails the whole extension load, with a structured error written
to `~/.axi/agents/loader/errors.jsonl`.

---

## 3. Lifecycle verbs

```bash
axi ext install <name>         # add to ~/.axi/extensions, run post-install hook
axi ext upgrade <name>         # replace, preserving config; drain-safe (§5.3)
axi ext uninstall <name>       # remove, run pre-uninstall hook
axi ext disable <name>         # remain installed but excluded from discovery
axi ext enable <name>
axi ext list [--json]
axi ext info <name>
```

Each mutator is **the only sanctioned path** for changing the installed-
extension set. Manual filesystem edits work but are detected only when
the optional watcher is active (§5.4); without the watcher, the user
must run `axi ext refresh` to invalidate the cache.

Each mutator publishes a typed event on the in-process event bus
(§4.2) before exiting.

---

## 4. ExtensionRegistry and the event surface

### 4.1 Singleton contract

`axiom.extensions.registry.ExtensionRegistry` is the in-process
authority. It exposes:

```python
class ExtensionRegistry:
    def list(self) -> list[Extension]: ...
    def get(self, name: str) -> Extension | None: ...
    def invalidate(self) -> None: ...   # forces re-discover on next access
    def subscribe(self, kind: str, fn: Callable[[ExtensionEvent], None]) -> None: ...
```

`get`/`list` reads are O(1) after the first discovery; `invalidate`
re-runs discovery on the next read.

### 4.2 Events

The bus is `axiom.infra.event_bus`. Publishers fire typed events; the
registry subscribes to lifecycle events and republishes a single
collapsed event for downstream consumers.

| Event | Publisher | Payload |
|---|---|---|
| `ext.installed` | `axi ext install` | `{name, version, source, ts}` |
| `ext.upgraded` | `axi ext upgrade` | `{name, from_version, to_version, ts}` |
| `ext.removed` | `axi ext uninstall` | `{name, version, ts}` |
| `ext.disabled` | `axi ext disable` | `{name}` |
| `ext.enabled` | `axi ext enable` | `{name}` |
| `extensions.changed` | `ExtensionRegistry` | `{added: [...], removed: [...], changed: [...]}` (collapsed; the only event downstream consumers should listen for) |

Downstream consumers subscribe to **only** `extensions.changed`. This
keeps the consumer count manageable and avoids races between the five
upstream events and the underlying cache.

### 4.3 Mandated subscribers

These platform components must subscribe to `extensions.changed` and
react on the next safe boundary:

| Component | Reaction |
|---|---|
| Argparse dispatcher | Re-binds noun → entry mapping from the new registry |
| Shell completion engine | Re-renders the completion-snapshot file used by frozen-binary fallback |
| Cross-harness shim generator (`axi commands`) | Calls `regenerate_all()` to refresh shims for previously-generated harnesses |
| Chat session (if active) | Updates the in-context noun-verb tree on next user turn |
| RIVET / TIDY / SCAN / TRIAGE daemons | Re-scan their hooks/agents on next heartbeat |
| Hook registry | Re-binds manifest-declared hooks (per `spec-hooks.md §7`) |
| Provider registry (LLM, gateway) | Re-scans for newly-provided MCP servers, providers |

A consumer that fails to react does not block other consumers.
Failures are logged to `~/.axi/agents/loader/errors.jsonl`.

---

## 5. Hot-Swap Protocol

### 5.1 Atomic install

`axi ext install` writes to a staging directory, validates the manifest,
runs the post-install hook in dry-run mode, and only then renames the
staging directory into place under `~/.axi/extensions/`. An interrupted
install leaves no partial state. The atomic-rename is the
discovery-visible commit point.

### 5.2 Drain semantics for in-flight invocations

When `extensions.changed` fires for a verb that is **currently
executing**:

- v1 (Python loader): the old code remains loaded in the in-flight call
  frame because Python references prevent GC. New invocations after the
  event resolve to the new module. There is no formal drain, but the
  Python reference model gives us "old finishes, new starts fresh" for
  free.
- v2 (WASM loader, see §6): in-flight invocations run to completion on
  the old WASM instance; new invocations bind to the new instance.
  Both instances coexist until the old finishes, then the old is
  destroyed. This is the formal drain.

### 5.3 What cannot be hot-swapped safely

The following changes require a process restart, signaled to the user
with a one-line "restart recommended" notice:

- **Changes to the dispatcher itself** (e.g. an extension that registers
  a hook on `cli.dispatch.before` whose handler changes shape).
- **Changes to the event bus topology** (e.g. an extension that
  introduces a new bus transport).
- **Changes to a daemon's manifest while that daemon is running and
  has open connections that depend on the old manifest's
  `deployment_profile`.**
- **Changes to a `kind=service` block when the service is currently
  serving traffic** — the service must be drained explicitly via
  `axi ext upgrade <name> --restart-service`.

Detection: the registry compares the new manifest's hash against the
old. If a sensitive section changed, the post-event handler emits the
restart notice (visible in the `axi ext install/upgrade` output and in
the next `axi chat` turn).

### 5.4 Filesystem watcher (opt-in)

For changes that bypass `axi ext` (manual filesystem edits, package
managers updating site-packages):

- **macOS / Linux**: `watchdog` library provides FSEvents/inotify
  observation of the discovery roots. Enabled by `axi config
  watcher.enabled = true`.
- **Windows**: `watchdog` uses ReadDirectoryChangesW.

When enabled, manifest mtime changes fire the same `extensions.changed`
event as the verb-driven path. Default: **disabled**. The watcher's
overhead is small but non-zero and the verb-driven path is sufficient
for the common case.

---

## 6. Forward-looking: WASM Component Model loader

### 6.1 Why

The hot-swap requirement (§5) is the architecturally clean entry point
for a WASM-backed loader. Python's import system was not designed for
safe reload — references accumulate, `importlib.reload` is partial,
side-effects of re-execution are unpredictable, and there is no per-
extension isolation. WASM Component Model fixes all of these:

- **True isolation**: a WASM instance cannot reach the host or sibling
  extensions except through declared WIT-typed bindings.
- **Native drain semantics**: instances coexist; in-flight calls
  complete on the old instance, new calls bind to new.
- **Polyglot**: extensions can be authored in any language that
  compiles to a WASM Component (Rust, Go, C/C++, AssemblyScript,
  py2wasm, Grain, etc.).
- **Federation-friendly**: signed WASM bytecode can travel between
  cohort nodes; receiving nodes execute it safely with only the
  capabilities the WIT declares. Composes with the trust graph
  (ADR-028) and AEOS Sigstore signing (§6).
- **Capability-based security**: WIT bindings make the per-extension
  host surface explicit and reviewable, complementing RACI.

### 6.2 Manifest field

```toml
[extension]
runtime = "python"   # or "wasm"   -- default: "python"
```

`runtime = "wasm"` requires a `wasm_module` path:

```toml
[extension.wasm]
module = "dist/extension.wasm"
component_model = "0.2"
wit_world = "axiom:extension/extension"
```

### 6.3 Host surface (WIT sketch)

```wit
package axiom:host;

interface memory {
    record fragment-write { ... }
    write: func(f: fragment-write) -> result<id, error>;
}

interface gateway {
    complete: func(prompt: string, opts: complete-opts) -> result<response, error>;
}

interface event-bus {
    publish: func(topic: string, payload: list<u8>) -> result<_, error>;
    subscribe: func(topic: string) -> stream<event>;
}

interface raci {
    propose: func(action: action-decl) -> verdict;
}

world extension {
    import memory;
    import gateway;
    import event-bus;
    import raci;
    export run: func(args: list<string>) -> exit-code;
}
```

The WIT world is the contract; only what an extension imports is what
it can touch. The host enforces capabilities at instantiation time —
e.g. an extension that doesn't import `gateway` literally cannot make
LLM calls.

### 6.4 Execution backbone

`wasmtime-py` (or equivalent) provides the in-process WASM runtime.
Per-invocation flow:

1. Resolve the noun → extension → WASM instance binding.
2. If no instance exists or the manifest changed since last
   instantiation, instantiate a new one (cold start ~5–20ms).
3. Marshal CLI args into the `run` function's `list<string>` arg.
4. Execute. Streaming output flows back via host-imported `stdout`.
5. On `extensions.changed` for an in-flight WASM extension, mark the
   instance for reaping; new invocations bind to the new instance.
   The reaper destroys the old instance once the in-flight call's
   future resolves.

### 6.5 Migration mechanics

- **v1 ships with `runtime = "python"` as the only supported value.**
  All current extensions are valid as-is.
- **v2 introduces `runtime = "wasm"`** behind a feature flag
  (`AXI_LOADER_WASM=1` or `axi config loader.wasm = true`). The
  Python loader remains the default.
- **Per-extension opt-in**: extensions migrate one at a time, declaring
  `runtime = "wasm"` in their manifest and shipping a `.wasm` artifact.
- **Performance-critical core paths** (the dispatcher itself,
  CompositionService, the trust graph) can be ported incrementally —
  each port gets its own ADR and migration branch.
- **Federation impact**: signed WASM extensions become transmissible
  via the federation layer; this requires a complementary update to
  `spec-federation.md` covering WASM-bytecode trust verification.

### 6.6 Out of scope for this spec

- The full migration plan and per-component ADRs (covered in their own
  docs once authored).
- Specific WIT version pinning and component-model version negotiation
  (deferred to the WASM-loader ADR).
- Signing pipeline for `.wasm` artifacts (extends Sigstore work in
  AEOS §6).

---

## 7. Open questions

1. **Watcher default**: should the filesystem watcher (§5.4) be on by
   default for `workstation` and `server` profiles, off only for
   `edge`?
2. **Restart-recommended visibility**: when a non-hot-swappable change
   is detected mid-`axi chat`, should the chat session surface the
   notice immediately or wait until the user finishes their current
   turn?
3. **WASM cold-start cost**: 5–20ms per cold instantiation may be
   acceptable for interactive verbs but not for hot-path daemon
   ticks. Evaluate caching strategies before the v2 ADR.
4. **Coexisting old + new instance windows**: how long do we keep an
   old instance alive after upgrade? Open-ended with reaper, or hard
   deadline (e.g. 5 minutes)?

---

## 8. Related documents

- [prd-axi-cli.md §Hot-load and hot-swap of extensions](../prds/prd-axi-cli.md)
- [prd-commands-generator.md](../prds/prd-commands-generator.md)
- [spec-aeos-0.1.md](spec-aeos-0.1.md)
- [spec-hooks.md](spec-hooks.md)
- ADR-028 (trust graph) — federation impact of signed WASM
- ADR-NNN (TBD) — WASM extension loader (post-Prague)

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs.
Apache-2.0 licensed._
