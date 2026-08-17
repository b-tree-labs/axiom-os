# ADR-036: Extension Runtime Surfaces + Install Slots

**Status:** Proposed (2026-04-29)
**Supersedes:** none (extends ADR-017 release pipeline; complements ADR-019 node profiles, ADR-022 federation identity, ADR-031 extension self-containment)
**Related:** ADR-017 (release pipeline + supply chain), ADR-019 (node profiles), ADR-022 (federation identity & membership), ADR-024 (root availability + delegation), ADR-031 (extension self-containment), `prd-canary-nodes.md` + `spec-canary-nodes.md` (channel taxonomy + canary protocol), `prd-federation.md §17` (16 install/upgrade scenarios), `prd-agents.md §Always-On Agent Services`, `spec-aeos-0.1.md` (extension manifests)

## Context

Axiom today silently assumes a singleton install per OS user: one `~/.axi/`, one set of launchd / systemd service labels, one venv resolution path, one node identity. That assumption holds for the canary fleet's reference machines (one Axiom per box) and for first-time installs. It breaks the moment the install becomes plural along any of the following axes:

1. **Install mode.** A developer running `pip install -e <repo>` against the workspace gets a live-reloading install where every code edit takes effect on the next agent tick. A canary running `pip install axi-platform==0.10.11` from PyPI gets a fixed-version install whose code only changes when the canary's promotion policy explicitly upgrades. A production node running the same wheel under the canary-validated `stable` channel gets a frozen install. These are operationally distinct surfaces with different live-reload semantics, different attribution requirements, and different appropriate behaviors — but Axiom currently has no first-class concept distinguishing them.

2. **Multi-instance on the same host.** A developer with a running canary on their workstation who wants to also work on repo-head edits has no clean way to do both. A second developer on the same shared lab box wants to install their own Axiom. A bare-metal node and a containerized canary on the same host both want to bind to the same well-known port. The current install pattern collides on `~/.axi/`, on launchd labels keyed by `package_name`, and on any host-network resource (LM endpoints, federation transport ports). The recently-formalized `axiom-aeos-tests/` worktree-with-its-own-`.venv` pattern (`project_axiom_aeos_worktree_venv.md`) is the lived precedent for this; it currently exists as convention, not as architecture.

3. **Forked identity from copy-install.** The federation threat model (`prd-federation §17.1 #5`) calls out forked identities — backup + restore mistakes producing two nodes with the same Ed25519 seed — as a federation-grade bug. The same failure mode applies trivially today to anyone who copies `~/.axi/` between two machines, or who restores a container image on a new host. There is no architectural mechanism to detect that two distinct installs share the same `node_id`.

4. **Extension developer concerns.** Extension authors today have no platform answer to: which surfaces does my extension support? When my manifest changes the `heartbeat_command`, why is my deployed launchd plist silently stale? Why does my agent fail under launchd but work under my shell (PATH not inherited)? When I file a heartbeat signal from my dev box, how do downstream consumers know that's not a production signal? These are not extension-specific bugs — they are platform-level concerns the extension framework should answer once for all extensions.

The end-to-end design study `working/rivet-lifecycle-2026-04-28.md` (this session) elevated these from an open question to a load-bearing differentiator. The decision recorded here makes that elevation architectural.

## Decision

Axiom introduces two orthogonal architectural concepts: **runtime surface** (which release-channel + install-mode the install runs under) and **install slot** (which resource-namespace this particular install instance occupies on its host). Both are mandatory metadata on every install; both propagate to attribution; both gate behavior at well-defined points.

### D1 — Three runtime surfaces

Every Axiom install runs under exactly one **surface** at any point in time. The surface is determined by *how* the install was created, not by what it does.

| Surface | Created by | Live-reload? | Code provenance | Default for |
|---|---|---|---|---|
| `editable` | `pip install -e <local checkout>` | **Yes** — every fresh interpreter sees current source | git working tree (may be dirty) | Workstation development |
| `edge` | `pip install axi-platform==<tag>` while tracking the canary `edge` channel | No | Signed wheel from PyPI; tag matches a published release | Canary nodes (per `prd-canary-nodes`) |
| `stable` | `pip install axi-platform==<tag>` while tracking `stable` | No | Signed wheel from PyPI; tag has been canary-validated and promoted | Production / customer nodes |

`edge` and `stable` are already defined by `prd-canary-nodes.md`. `editable` is new — it formalizes the live-reload-from-local-checkout case as a peer of the two channel-tracking modes, instead of leaving it as an unmodeled escape valve.

Reserved sibling names exist for future modes that don't fit (`vcs-source` for `pip install git+https://…@<ref>`, `artifact-source` for unpublished wheels) but are deliberately not first-class today; the canary `edge` channel covers the operational version of those needs.

### D2 — The surface is detectable, declared, and immutable per process

`axi surface status` resolves the current surface deterministically:

```
1. Inspect importlib.metadata.distribution("axi-platform").read_text("direct_url.json")
2. If dir_info.editable == True → surface = `editable`
3. Else if upgrade-policy state file says channel == "edge" → surface = `edge`
4. Else → surface = `stable`
```

The result is cached for the lifetime of the process — surface does not change mid-process. A surface change requires a fresh interpreter (consistent with ADR-017's release-pipeline assumptions).

### D3 — Install slot is the resource-namespace unit

An **install slot** is the unit of host-resource ownership for a given install instance. Slot identity is `slot_id = H(venv_path || state_dir)` — and *only* those two inputs. Hostname is deliberately excluded: classified-facility hostnames often encode project/vault metadata, and mixing hostname into a hash that propagates to attestations would create a confirmation-oracle adjacency leak (an attacker who guesses the facility's hostname pattern can verify any `slot_id`). Slot *name* defaults to `default` (preserving today's behavior fully) and is otherwise derived from the venv path's last segment. Two installs on the same host are guaranteed-distinct iff they have distinct `(venv_path, state_dir)` pairs.

Every host-touching resource is namespaced by slot:

| Resource | Default-slot pattern (today) | Slot-aware pattern |
|---|---|---|
| State directory | `~/.axi/` | `~/.axi/<slot>/` (default slot keeps `~/.axi/` directly for back-compat) |
| launchd label | `com.axi-platform.<agent>-agent` | `com.axi-platform.<slot>.<agent>-agent` (default slot keeps unprefixed form) |
| systemd unit | `axi-<agent>-agent.service` | `axi-<slot>-<agent>-agent.service` (default slot unprefixed) |
| Listener ports | hardcoded defaults | per-slot port range allocated at install time, recorded in slot manifest |

Default-slot back-compat is mandatory: existing installs continue to work without any user action. The slot prefix only appears when more than one slot exists on the same host.

### D4 — Slot identity is orthogonal to node identity, not mixed into it

`node_id` derivation is unchanged: `node_id = H(seed_keypair_pubkey)`. Slot identity is **not** mixed into the hash. Earlier drafts proposed `node_id = H(pubkey || slot_id)` to make copy-install safe-by-construction, but doing so creates a permanent federation split-brain — every peer would need to support both pre- and post-ADR-036 derivation functions forever to validate signatures, and the two derivations would be indistinguishable without an additional metadata bit.

Instead, slot identity is carried as a **separately-signed claim** in the federation membership envelope, signed by the same Ed25519 key as the `node_id`:

```python
@dataclass(frozen=True)
class SlotClaim:
    node_id: str
    slot_id: str             # H(venv_path || state_dir)
    install_id: str          # H(slot_id || surface), stable per-install
    signed_at: datetime
    signature: bytes         # Ed25519(node_id || slot_id || install_id || signed_at)
```

This gives peers everything needed to disambiguate without changing the derivation function. The trade-off: copy-install of `~/.axi/` to another host produces a *valid* `node_id` (the seed is the same), but the `SlotClaim` reveals the duplication if the original host is also publishing a `SlotClaim` for the same `node_id` with a different `slot_id`. Cross-host detection is a federation-side concern (peers reporting which `(node_id, slot_id)` pairs they've seen) and is tracked as a follow-on ADR — see "Open items" below. Within this ADR, slot identity is local-only enforcement.

The legitimate "reinstall from backup" case (`prd-federation §17.1 #10`) is unchanged: it requires the resumption-statement ceremony already specified in ADR-022 — explicit, audited, signed — which now also re-signs the `SlotClaim` for the new `(venv_path, state_dir)`.

**Limitation acknowledged:** This design prevents *accidental* same-host slot collision (F8) and gives peers a tool to detect *cross-host* duplication after the fact, but it does NOT prevent a determined attacker who steals the seed key from signing federation messages from another host under the original `node_id`. Seed-key theft remains the threat-model boundary; mitigations (HSM-backed seeds, periodic rotation) are out of scope for this ADR.

### D5 — Multi-instance on the same OS is supported, not warned-against

The four scenarios from `prd-extension-runtime-surfaces.md §"Multi-instance on the same OS"` (same human two installs; different humans same host; dev work alongside running node; containers vs bare metal) are all *supported configurations*, not edge cases. Slot-aware naming + dynamic port allocation + per-slot state directories are sufficient. The convention is that each install gets its own venv (the worktree-venv pattern from `project_axiom_aeos_worktree_venv.md`); the platform detects and refuses the unsafe variant (two installs sharing a venv overwrite each other on `pip install -e .`).

### D5a — Per-surface key handling: editable installs refuse production keys

A developer cloning their `stable` node's `~/.axi/` into an `editable` workstation slot would inherit the production seed — which is wrong on its face: developer machines should not be able to publish federation messages under a production identity. The platform enforces:

- On surface = `editable`, the identity loader **refuses** to load any `seed_keypair.pem` whose accompanying `SlotClaim` was signed under a non-`editable` surface. Loud refusal at startup, with guided remediation (re-init this slot's identity, or move the slot to a different venv).
- On surface = `editable`, federation publish operations may be configured per-cohort to refuse outgoing messages entirely (per-cohort policy, default off pre-Phase-3, default on post-Phase-3).
- On surface = `stable` or `edge`, identity loading is unchanged.

This is a deterministic gate — not a heuristic. It runs during `axi nodes load` regardless of LM availability.

### D5b — Cross-slot federation membership defaults to forbidden

Two slots on the same host could be peers in the same federation cohort. Without a default, each peer would decide differently and federation behavior would be non-uniform. The default is: **peers refuse to admit two slots from the same host into the same cohort** (detected via matching transport-key fingerprints OR matching outbound IP within a recent window). Operators may override on a per-cohort basis with explicit `axi federation allow-co-resident-slots <cohort_id>` and a recorded justification. The override is gossipped to peers as an `co_resident_slot_admitted` attestation per ADR-024 §revocation channel, so the override is never silent.

### D6 — Surface and slot are propagated to attribution, and surface claims carry signed evidence

Every signal, log entry, attestation, and federated message carries:

```python
@dataclass(frozen=True)
class InstallContext:
    surface: Literal["editable", "edge", "stable"]
    slot: str                        # default = "default"
    install_id: str                  # H(slot_id || surface), stable per-install
    surface_evidence: SurfaceEvidence  # NEW — signed proof of surface claim
```

**A surface claim without signed evidence is a hint, not a security control.** A malicious peer could simply claim `surface=stable` while running `editable`. To prevent this, `SurfaceEvidence` carries cryptographic proof:

```python
@dataclass(frozen=True)
class SurfaceEvidence:
    # For surface in {edge, stable}: hash of the installed wheel + sigstore signature.
    # For surface == editable: an explicit signed declaration that NO signed wheel
    # is present (the absence proof itself is signed by the local Ed25519 key).
    wheel_sha256: str | None         # None iff surface == editable
    sigstore_bundle: bytes | None    # None iff surface == editable
    editable_attestation: bytes | None  # signed declaration for editable; None otherwise
```

Receiving peers verify: for `edge`/`stable`, the wheel hash must resolve to a published Sigstore bundle for `axi-platform==<version>`. For `editable`, the absence-of-wheel claim is signed by the local key — peers can choose to accept or reject this attestation under their own trust policy.

This is appended to `MemoryFragment.provenance` as a non-breaking optional field, and to RIVET's heartbeat JSONL (and all other agent telemetry).

**Classification redaction at federation gateway.** When InstallContext crosses a federation boundary (cohort A → cohort B) per ADR-027, the gateway applies redaction:

- `surface` and `surface_evidence` are always preserved (they are the trust signal).
- `slot`, `install_id`, and any path-fragment metadata are redacted unless the cohort policy explicitly authorizes export. Default: redacted. Rationale: `slot` and `install_id` are correlation handles that may reveal deployment topology; `surface` alone is sufficient for cross-cohort trust evaluation.
- The redaction itself is recorded in the gateway audit log so the originating cohort can verify their topology was protected.

### D7 — Extension manifests may declare supported surfaces

`axiom-extension.toml` gains an optional block:

```toml
[extension.surfaces]
supports = ["editable", "edge", "stable"]   # default if omitted: all
```

`axi ext lint` warns on extensions that declare narrower support than they actually exercise (a heuristic, not a gate). The platform refuses to load an extension on a surface it has explicitly declared unsupported — this is the deterministic gate.

### D8 — Drift between deployed and declared state is detectable, and `--heal` is bounded

Today, an extension author who edits their manifest's `heartbeat_command` finds that the deployed launchd plist still fires the old command until someone manually re-registers. This is silent staleness — exactly the class of bug `prd-federation §17.1 #2` ("partially functional nodes") warns against.

`axi agents drift` is a deterministic check: for every registered service, compare deployed ExecStart / unit content against what the current manifest would generate. Mismatches print a remediation plan. The check is fast enough to run on every `axi agents status` invocation and is added there as a one-line warning when drift is present.

**`--heal` is bounded** — without bounding, `--heal` is a path from "edit any file in the working tree" to "rewrite my launchd plist," which is exactly the class of supply-chain hole this ADR is supposed to close. The bounded contract:

- On surface = `stable` or `edge`: manifest comes from a signed wheel; `--heal` may proceed.
- On surface = `editable`: `--heal` requires the relevant manifest fields (`heartbeat_command`, `heartbeat_interval`, `entry`) to match a content-hash recorded at the last explicit `axi agents register` invocation. If the hashes diverge, `--heal` refuses and prints the diff plus the explicit re-register command; the operator must consciously re-register, not just heal.
- Across classification boundaries: `--heal` partitions by classification level; remediation across multiple classification levels in one operation is refused. Per-level authorization is required (this is Phase 3 work; pre-Phase-3, all extensions are uniform-classification).

### D9 — Service environment is platform-managed, bounded, and minimal

The launchd / systemd / Windows Task Scheduler env-injection contract is platform-defined and *bounded*. Naively passing through `os.environ` at install time creates two problems: (a) it persists shell PATH quirks (`.` in PATH, writable `/tmp/bin`) into a plist that survives reboots — a TOCTOU exfiltration channel; (b) it bakes the operator's session paths into a forensic artifact at a known location, which is a topology-leak in classified contexts.

The contract:

- **PATH** is the intersection of `os.environ["PATH"]` at install time with a curated allow-list:
  ```
  /usr/bin
  /usr/local/bin
  /opt/homebrew/bin              # macOS
  /opt/homebrew/sbin             # macOS
  ~/.local/bin                   # user-local
  <venv>/bin                     # the install's own venv
  ```
  Any path not on the allow-list is dropped. `.`, `~`, and any world-writable directory (mode `& 0o002`) are refused with a loud warning at install time. The resulting PATH is recorded in the slot manifest so `axi agents status` can show it on demand.
- **HOME is NOT default-injected.** A daemon does not need access to `~/.ssh`, `~/.aws`, `~/.config/gh`, etc. by default. Extensions that need a specific config path declare it explicitly in the manifest:
  ```toml
  [agent.env_inputs]
  config_paths = ["~/.config/gh/hosts.yml"]   # platform binds these as env vars
  ```
  The platform reads each declared path at install time and exports its contents (or path) as a discrete env var. The agent gets exactly what it declared, not the whole HOME.
- **LANG** and **LC_ALL** are passed through as-is from `os.environ` (no security implications; needed for Python's stdout encoding).
- All other env is empty unless declared in `[agent.env]`.

Extensions do not need to know the difference between the three providers. This is the in-flight F2 work; the spec records the full contract.

### D10 — Platform-managed services run under a hardened sandbox profile by default

A daemon agent that polls external APIs and runs heuristic LLM-mediated diagnosis should not have unbounded read access to the user's filesystem. The platform ships per-provider hardened defaults:

- **Linux (systemd):** `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=read-only` by default (extensions may relax to `ProtectHome=tmpfs` or off via manifest), `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `RestrictNamespaces=true`, `LockPersonality=true`, `MemoryDenyWriteExecute=true`. State writes are confined to the slot's state directory via `ReadWritePaths=`.
- **macOS (launchd):** `ProcessType=Background`. Sandbox-exec profile generation is hooked in but ships with a permissive default-allow profile in Phase 0; the hook exists so Phase 2/3 can author tighter profiles without retrofitting. The platform records the hook point and emits a warning when no tightened profile is loaded.
- **Windows:** declared TODO in Phase 0; AppContainer integration tracked as follow-on work.

Manifests may relax (but not remove) the defaults via an explicit declaration:

```toml
[agent.sandbox]
protect_home = "tmpfs"             # default: read-only
read_write_paths = ["~/.config/gh"]  # explicit allow-list of state-dir-external paths
private_network = false            # default: true (no IPC sockets to other agents)
```

Each relaxation is logged at install time and surfaced in `axi agents status`. Extensions cannot escape the sandbox via runtime configuration — only via manifest declaration that the operator can audit before install.

### D11 — Editable surface is refused in classified workloads

Uncommitted code is unattested code. In a classified context, `editable` is a software-supply-chain hole — there is no signed wheel, no Sigstore bundle, no provenance other than the working tree's git commit (which may be dirty, or a branch the team has not reviewed).

Therefore: **classified workloads MUST run on `stable` or `edge`. The platform refuses to load any classification-marked extension on surface = `editable`, at policy-evaluation time, deterministically.** No flag, no override; the only way to run classified work on `editable` is to first commit + tag + publish the code, then track that tag.

This is the load-bearing classification-policy hook for surface attribution. It binds the surface concept to `spec-classification-boundary.md`'s refusal semantics: a fragment with non-empty `classification` cannot be written from a process whose `InstallContext.surface == editable`.

## Consequences

**Positive:**
- Multi-instance same-OS is supported by design, not by hoping nobody collides.
- Forked-identity-from-copy-install becomes architecturally impossible without explicit ceremony.
- Surface attribution lets federated peers and the canary subsystem reason about signal provenance.
- Drift detection closes the silent-stale-service class of bugs platform-wide, not per-extension.
- Cross-platform service env hygiene is a single platform contract instead of per-extension shell-pasting.
- Worktree-venv pattern in memory becomes load-bearing convention with platform support.

**Negative / costs:**
- Three concepts to learn (surface, slot, install_id) where one (the install) sufficed before. Mitigated by: default slot is invisible; surface is auto-detected; install_id is never user-typed.
- Manifest gains optional blocks (`[extension.surfaces]`, `[agent.sandbox]`, `[agent.env_inputs]`); back-compat preserved by sensible defaults.
- Service naming gets a slot prefix in non-default cases; tooling that grepped for `com.axi-platform.<agent>-agent` literally must be updated.
- Federation peers running pre-ADR-036 versions cannot consume the new attribution fields. Treated as an additive schema change with reader fallback (peers that don't understand `InstallContext` ignore it; matches `MemoryFragment` schema-evolution discipline).
- Sandbox defaults will surface real bugs in extensions that quietly read paths outside their state dir. Mitigated by: clear error messages, manifest-level relaxation path, audit log of all relaxations.
- The bounded PATH allow-list will fail-loud when an operator's tooling lives in a non-allow-listed directory (e.g., `pyenv` shims at `~/.pyenv/shims`). Mitigated by: documented allow-list, explicit add via manifest `[agent.env]`.

## Threat model

Threats this ADR closes:

| Threat | Closed by |
|---|---|
| Same-host accidental slot collision (two installs sharing state dir / service labels) | Slot-aware resource namespacing (D3); F8 same-host guard |
| `editable` install silently treated as production-equivalent for federation trust | Signed surface evidence (D6); per-surface trust policy hooks |
| Classified workload running from uncommitted code | D11 deterministic refusal of `editable` for classified |
| Drift between manifest and deployed service silently rotting | D8 drift detection + bounded `--heal` |
| Daemon agent reading arbitrary user secrets (`~/.ssh`, `~/.aws`) | D9 minimal env contract (HOME not default-injected); D10 sandbox-by-default |
| Captured PATH includes attacker-writable directories | D9 bounded PATH allow-list |
| Hostname-derived `slot_id` leaking facility classification adjacency | D3 hostname excluded from `slot_id` |
| Topology leak via federated InstallContext | D6 redaction at federation gateway |

Threats acknowledged but not closed by this ADR (tracked as follow-on work):

| Threat | Why deferred |
|---|---|
| Cross-host seed-key theft producing forked identity | Requires peer-side gossip of `(node_id, slot_id)` pairs and federation-wide collision detection. Separate ADR planned post-Prague (`feedback_freeze_foundation_during_delivery`). The seed-key remains the threat-model boundary; HSM-backed seed storage and rotation cadence are separate concerns. |
| `axi agents drift --heal` partitioned remediation across classification levels | Phase 3 work; pre-Phase-3 deployments are uniform-classification, so the partitioning is a Phase 3 spec amendment, not a Phase 0 omission. |
| Sandbox profile authoring on macOS sandbox-exec | Phase 2/3 work; the Phase 0 hook exists so profiles can be tightened without retrofitting the service generator. |
| AppContainer integration on Windows | Phase 3+; Windows is not a Phase 0/1 platform. |
| Slot port-allocation race (parallel `axi agents register` from two shells) | Spec'd in tech spec via O_EXCL claim file; implementation lands in Phase 2 alongside slot first-class CLI. |

## Operational risks (not security)

| Risk | Mitigation |
|---|---|
| Operators surprised by per-slot state-dir paths after creating a second slot | `axi slot status` always shows the resolved state_dir; first-creation of a non-default slot prints the new path explicitly |
| Extension declares narrow `[extension.surfaces]` and breaks under a real-world install we didn't anticipate | Default = all surfaces; declaration is opt-in; `axi ext lint` warning, not gate |
| Drift detector false positives during in-flight upgrades | Detector is read-only; remediation requires `--heal` flag; never auto-modifies during status checks |
| Bounded PATH breaks tools installed via `pyenv`, `asdf`, etc. | Documented allow-list; explicit add via manifest `[agent.env]`; install-time loud warning when a captured path is dropped |
| Sandbox defaults break extensions that quietly read paths outside their state dir | Clear error message identifies the path; manifest relaxation is a documented one-line edit; audit log records all relaxations |
| Container-installed Axiom claims a slot that overlaps with bare-metal install on host net | Container slot resolver inside the container is independent (`default` from container's POV); host-net binding requires explicit `--slot` claim with port-range arbitration |

## Compliance gates introduced

- `pytest -m surface_compliance` (new marker):
  - Every install resolves a deterministic surface.
  - Every install resolves a deterministic slot.
  - `node_id` derivation is unchanged; `SlotClaim` carries slot identity as a signed sibling field.
  - Provenance fragments carry `InstallContext` once schema bump lands.
  - Drift detector returns clean state immediately after `axi agents register`.
  - PATH allow-list test: `.`, `~`, and any world-writable directory are refused at install time.
  - HOME-not-default test: a daemon launched via the platform's service generator does not see `~/.ssh`, `~/.aws`, etc., unless declared in `[agent.env_inputs]`.
  - Sandbox-default test: systemd unit emits `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome=read-only` unless explicitly relaxed.
  - `editable`-classified-refusal test: writing a classification-marked fragment from a process where `surface == editable` is rejected at write time.
  - `editable`-key-handling test: loading a `seed_keypair.pem` whose `SlotClaim` was signed under `surface != editable` is rejected on `surface == editable`.
  - Federation-gateway-redaction test: InstallContext crossing a cohort boundary has `slot` and `install_id` redacted unless explicitly authorized.

These join `accountability_compliance` (ADR-035) and `pipeline_compliance` (ADR-034) as release gates.

## Phasing

- **Now (Phase 0 — this PR):** This ADR + PRD + Spec + finish F2 (cross-platform service env injection).
- **Phase 1 (next milestone):** F1 (`axi surface status`), F3 (surface attribution on heartbeat), F4 (`axi agents drift`).
- **Phase 2:** F5 (manifest `[extension.surfaces]`), F6 (`axi slot status`), F7 (slot-aware service naming).
- **Phase 3:** F8 (forked-identity guard), `node_id` derivation update with migration, federation peer-side surface-aware trust profiles.

Phase 0 ships the foundation without forcing any user-visible change. Phases 1–2 deliver the developer-facing features. Phase 3 lights up the federation half — gated on Prague-class deployments stabilizing first per `feedback_freeze_foundation_during_delivery.md`.

## Open items

- **Slot lifecycle CLI surface.** F6 ships read-only `axi slot status / list`. Whether `axi slot create / use / delete` becomes first-class depends on whether multi-slot installs become common enough to warrant explicit management; until then, slot creation is implicit (creating a new venv + state dir is creating a new slot).
- **Cross-host forked-identity detection ADR.** D4 closes same-host accidental collision but defers cross-host seed-key theft to a follow-on ADR. Mechanism sketched there: peers gossip the `(node_id, slot_id, transport_key_fingerprint, first_seen_ts)` tuples they've observed; the federation flags any `node_id` seen with two distinct `slot_id`s within a recent window. Post-Prague.
- **Sandbox profile authoring.** Phase 0 ships systemd hardening directives; macOS sandbox-exec emits a permissive default profile with the hook in place. Phase 2/3 authors tighter profiles per agent class (e.g., a different sandbox for RIVET, who polls outbound HTTPS, vs. TIDY, who only writes to state dir).
- **Telemetry of surface mix in the wild.** Should the platform optionally report per-surface usage to the canary-attestation sink for ecosystem health? Privacy-sensitive; defer until canary attestation has matured.
- **HSM-backed seed storage.** Out of scope for this ADR; tracked as an independent security workstream.

## The bottom line

Axiom today supports one install per OS user, one channel per install, and trusts the OS to keep things separate. This ADR makes plurality first-class — multiple installs, multiple surfaces, multiple slots — without forcing complexity on the singleton case. The cost is three concepts and a manifest block. The value is that the four multi-instance scenarios that already happen in practice stop relying on luck, and the silent-staleness class of bugs (drift, PATH, forked identity) becomes detectable by construction.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
