# Agent sandboxing — what is enforced, and what an extension may ask for

**Status:** Current as of 2026-10-02 (verified against the code, not the ADR)
**Decisions:** [ADR-036 §D10](../adrs/adr-036-extension-runtime-surfaces.md),
[ADR-155](../adrs/adr-155-an-agent-may-contain-a-leak-before-it-may-manage-a-key.md)
**Audience:** anyone asking "how are your agents sandboxed?", and extension
authors who need to know what the host will and will not let their agent do.

This document states what the platform actually enforces. Where a control is
absent it says so plainly, because the question this answers is a security
question and a hedged answer is worse than a negative one.

## 1. The short answer

A platform-managed agent runs as a **user-level service** — never root —
under hardening the host applies, not the extension. On Linux that hardening
is real and enumerated in §3. **On macOS there is none**, and on Windows it
is not yet built. An extension can ask the host to relax two axes and to
tighten one; it cannot reach anything else, and a request wide enough to
defeat the floor is refused when the unit is generated rather than written
into a unit that survives reboots.

## 2. Why this is a composability problem

A deployment is a host plus extensions the host's author did not write and
may not have seen. The host cannot enumerate what those extensions will do,
so the boundary cannot be a list of permitted behaviours. It is instead:

- **A floor the host sets and no extension can lower.** Not negotiable, not
  declarable, not reachable at runtime.
- **A small set of axes an extension may DECLARE**, in its manifest, where
  the host can read the declaration before installing anything.
- **Refusal at generation time** for a declaration that would defeat the
  floor — not a warning, and never silently ignored. Ignoring a declaration
  applies the default while the manifest claims otherwise, which leaves an
  operator auditing a statement that was never true.

The agreement between host and extension is therefore a *manifest*, not a
runtime negotiation. An operator can read what an extension will be allowed
to do before it is installed, and nothing the extension does later changes
it.

## 3. What the floor is, per platform

### Linux (systemd) — enforced

Emitted into every platform-managed unit. No manifest can remove or weaken
any of these:

| directive | what it stops |
|---|---|
| `NoNewPrivileges=true` | gaining privilege via setuid |
| `PrivateTmp=true` | reading or planting files in a shared `/tmp` |
| `ProtectSystem=strict` | writing anywhere in `/`, `/usr`, `/boot` |
| `ProtectHome=read-only` | writing to the home directory (**reading is allowed** — see §5) |
| `ReadWritePaths=<state dir>` | writing outside its own state directory |
| `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` | raw/packet sockets and exotic families |
| `RestrictNamespaces=true` | building a namespace to escape from |
| `LockPersonality=true` | personality-based evasion |
| `MemoryDenyWriteExecute=true` | W^X violations — JIT-ROP and similar |

Units install via `systemctl --user`, so an agent runs **as the invoking
user and not as root**. This is load-bearing for anything that reads process
state: the credential sweep's `/proc/<pid>/environ` probe sees that user's
own processes and is denied the rest by the kernel, not by our code.

### macOS (launchd) — NOT enforced

The plist carries `ProcessType=Background` and `LowPriorityIO`. These are a
scheduling QoS class and a disk-priority hint. **Neither is a security
boundary.** There is no `sandbox-exec` profile, no `SandboxProfilePath`, and
no hook — only a `# Future:` comment where one would go.

An agent on macOS can read anything its user can read and open any socket
its user can open. Install emits a warning saying so, and
`axi agents status` reports `Sandbox: NOT APPLIED`.

This matters more than its share of hosts suggests, because macOS is the
platform most development is done on. A control that exists in production
and not on the machine where behaviour is designed is a control nobody
exercises.

### Windows — not built

Declared TODO; AppContainer integration is follow-on work.

## 4. The axes an extension may declare

```toml
[agent.sandbox]
protect_home = "tmpfs"               # relax: default "read-only"; also "off"
read_write_paths = ["~/.config/gh"]  # relax: adds to, never replaces, the state dir
network = false                      # TIGHTEN: default true
```

**`protect_home`** — an unknown value is refused, not ignored.

**`read_write_paths`** — added to the state directory, never substituted for
it; an agent that gained a config path and lost its own state directory
could not run. A path broad enough to defeat `ProtectSystem` is refused when
the unit is generated: *a relaxation wide enough to remove the sandbox is a
removal wearing a relaxation's clothes.*

**`network`** — the only axis that tightens, and the only one where an
extension gives something up. `false` emits `PrivateNetwork=true`,
`RestrictAddressFamilies=AF_UNIX` and `IPAddressDeny=any`. Setting it to
`true` buys nothing: no manifest may widen past what the host already
grants.

Every relaxation is logged at install and surfaced in `axi agents status`,
so "what did this extension ask for" is answerable after the fact without
reading its manifest again.

## 5. The limit worth understanding: the floor bounds writes, not reads

`ProtectHome=read-only` is precisely a grant to read the home directory.
For most daemons that is the right shape — the hazard is corruption and
persistence. For an agent whose job is to **read** (a credential scanner, a
log analyser, an indexer) it is the wrong axis, because such an agent's
whole risk is egress.

Two things carry that case instead of the filesystem floor:

1. **`network = false`.** A local scanner has no reason to hold a socket. An
   agent that both scans and calls out cannot use it — KEEP is the example:
   its `outbound_call` is the platform's only plaintext-credential site and
   needs HTTP, so the scanning half would have to become its own unit first.
2. **What the output carries.** The credential sweep's findings are a
   locator, a matcher name and a truncated SHA-256. No credential value
   leaves the scan, by construction, so a discovery report is safe to paste
   into a ticket. This is the control that holds on macOS, where the
   filesystem floor does not.

## 6. Answering the question when asked

- *How are agents sandboxed?* — User-level service, never root. On Linux, the
  systemd table in §3, applied by the host and not removable by an
  extension. On macOS, not at all today, and we say so at install.
- *What stops an extension escaping?* — The floor is not declarable, and a
  declaration wide enough to defeat it is refused at unit generation. There
  is no runtime path that widens the sandbox.
- *You don't control the extensions. How can you promise anything?* — The
  promise is the floor, which is the host's, plus a manifest an operator can
  read before installing. We do not promise anything about what an extension
  *does*; we constrain what the host will let it do.
- *What about scanning for secrets?* — §5. The filesystem floor does not bound
  reads, deliberately; the controls are no-network and never emitting a
  value.
- *What is the weakest part?* — macOS, and we would rather say it than be
  found out. The next two pieces of work are splitting the scanning half of
  KEEP into a network-free unit, and authoring the `sandbox-exec` profile.

## 7. Known gaps (disclosed)

| gap | status |
|---|---|
| macOS has no confinement | known; warned at install; profile is follow-on work |
| Windows has no confinement | known; AppContainer is follow-on work |
| The sweep cannot use `network = false` | it runs inside KEEP, which needs HTTP; needs its own unit |
| The floor does not bound reads | deliberate; see §5 for what covers it instead |
