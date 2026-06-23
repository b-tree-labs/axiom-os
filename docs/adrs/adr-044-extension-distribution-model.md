# ADR-044: Axiom Extension Distribution Model — Standalone-or-Builtin Decision Rule

**Status:** Proposed (2026-05-05)
**Supersedes:** none
**Generalizes:** ADR-018 (each physics code is its own AEOS-conformant extension; physics = native)
**Related:** ADR-031 (extension self-containment — docs + tests co-located with code), ADR-032 (standards positioning — public AAIF + private AEOS), ADR-040 (compute decomposition — first multi-extension primitive consumer), ADR-043 (RACI evolution — agent autonomy state machine).
**Refined by:** ADR-048 (brand-scoped extension visibility — discovery stays universal per D2.6; what each *brand* displays is scoped by tier).
**Specs:** `spec-aeos-0.1.md`, `spec-aeos-1.0.md`.
**Memory ground:** `feedback_axiom_extension_distribution_model`, `project_axiom_co_ownership`, `reference_gitlab_pat`.

This ADR is a *language-agnostic* architectural decision. AEOS extensions can be written in Python, Rust, Go, TypeScript, or any language whose ecosystem can express the architectural primitives below. Python is presented throughout as the **canonical first instantiation** because it is the language of the first complete extension stack (axiom-os, a domain consumer, a physics-code adapter extension), but the rule and the contract apply identically to bindings in other languages once those bindings exist.

---

## Context

Axiom's extension surface has grown across two axes: capability kinds (`agent`, `tool`, `cmd`, `service`, `adapter`, `skill`, `hook` per AEOS spec §5) and consumer domains (a domain consumer, Keplo for classroom, Vega for federation governance, Vyzier for marketplace, etc.). Three different distribution patterns are in use today, none of them governed by an architectural rule:

1. **Builtin to a consumer** — co-located in the consumer's main repository, ships with the consumer's release cadence, co-versioned with consumer core.
2. **Builtin to axiom-os** — co-located in axiom-os, ships with axiom-os's release cadence, available to every consumer that depends on axiom-os.
3. **Standalone repo, sibling distribution** — a separate package living inside the axiom-os monorepo's package directory, published as its own artifact.

The third pattern has so far been an *implementation detail*. This ADR makes the distribution choice an *architectural decision* with a clear rule, because consumer extensions of different generality need different release cadences, governance owners, and discovery paths.

The triggering case is the OpenMC physics-code extension. Per ADR-018 it must be its own AEOS-conformant extension. Per the rule formalized below, *specific-to-one-code* extensions ship as fully-standalone repositories (own version control, own CI/CD, own release cadence, own ownership boundary) — not as packages inside axiom-os. This ADR generalizes that rule to every Axiom extension going forward.

---

## Decision

Every Axiom AEOS extension that will be open-sourced is **either** a builtin of a consumer **or** a standalone repository, decided by a single rule of generality. Apache-2.0 is the universal license; the AEOS manifest is the universal interface; the difference is *distribution shape*.

### D1 — The decision rule

For a candidate extension, ask the rule of generality:

> *"Is this extension useful for ALL facilities/instances within the consumer's domain AND useful across ALL backends/codes/sources within that domain?"*

| Answer | Distribution |
|---|---|
| **Yes (universal within the domain)** | **Builtin** to the consumer's main repo |
| **No (specific to one code, backend, source, or facility)** | **Standalone** repository under the consumer-domain organization, with its own version control, CI/CD, and language-ecosystem release cadence |

Examples:

| Extension | Consumer | Rule outcome | Why |
|---|---|---|---|
| Hygiene / drift dashboard | axiom-os | Builtin | Every consumer benefits regardless of domain or backend |
| Classroom (lecture/quiz/cohort) | Keplo | Builtin to Keplo | Every classroom consumer needs it; not specific to one course or LMS |
| OpenMC physics-code adapter | a domain consumer | **Standalone** | Specific to one physics code; not every consumer install needs it |
| MPACT physics-code adapter | a domain consumer | **Standalone** | Same rule; different code; future case |
| Canvas LMS adapter | Keplo | **Standalone** | Specific to one LMS; Moodle/Blackboard would each be separate |

Per D6 below, *bias toward standalone when in doubt* — standalone wrappers reach every Axiom-enabled host on the network; builtins only reach the consumer they ship with.

### D2 — Standalone-repo conventions

When the rule says "standalone," the extension MUST satisfy these architectural conventions for v1. Each is stated as a **language-agnostic requirement**, with an **example (Python)** showing the canonical first realization.

#### D2.1 — Primary version-control repo under the consumer's domain organization

The repository's GitHub (or equivalent SCM) owner is the consumer's domain organization, *not* the platform organization. This signals provenance + governance.

> *Example (Python).* A domain physics-code extension lives at `example-org/axiom-ext-openmc`, not at `b-tree-labs/axiom-ext-openmc`, even though core Axiom lives under `b-tree-labs/`.

#### D2.2 — Mirror to the consumer's secondary code-hosting surface

A push-mirror is configured from the primary SCM to whatever secondary surface the consumer's downstream partners require (gov labs, EC-routed environments, air-gapped sites, etc.). This is a hard requirement — some downstream consumers cannot reach public GitHub but can reach an institutional GitLab or equivalent.

> *Example.* Domain extensions push-mirror GitHub → an institutional GitLab.

#### D2.3 — Independent CI/CD

The repo has its own continuous integration (lint, test) and continuous-delivery (build, publish) pipelines, parameterized to the consumer-domain's organization. Release cadence is independent of the platform's release cadence.

> *Example (Python).* `.github/workflows/ci.yml` runs ruff + pytest across the supported runtime versions; `.github/workflows/publish.yml` triggers on tag-push and publishes to PyPI.

#### D2.4 — Language-ecosystem release cadence under the consumer's namespace

The extension is published to the appropriate language-ecosystem registry (PyPI for Python, crates.io for Rust, npm for Node, etc.) **under the consumer's organization** in that registry — not under the user's personal namespace.

> *Example (Python).* `axiom-ext-openmc` is published to PyPI under the UT-Computational-NE PyPI organization. The PyPI org's pending-publisher (or trusted publisher) is configured before the first release tag, so the project is born inside the org rather than transferred later.

#### D2.5 — License + NOTICE

Apache-2.0 is the universal license. The NOTICE captures consumer-domain organization + B-Tree Labs co-ownership where applicable per `project_axiom_co_ownership`, plus attribution for the wrapped tool and any dependencies.

#### D2.6 — Discovery via the language-ecosystem's native registration mechanism

The package declares the capability it provides under the appropriate AEOS capability group, using the language ecosystem's native plug-in/registration mechanism. The architectural requirement is: **installing the package alone is the integration step.** No manual import/load on the consumer's part. No manifest hand-editing.

The mechanism is language-specific; the requirement is universal.

> *Example (Python).* Python expresses this via `[project.entry-points."<group>"]` in `pyproject.toml`. For a kernel adapter:
>
> ```toml
> [project.entry-points."axiom.compute.adapters"]
> openmc = "axiom_ext_openmc.adapter:OpenMCKernelAdapter"
> ```
>
> `axiom.compute.adapters.get_adapter("openmc")` lazy-loads on first miss and finds the adapter via `importlib.metadata.entry_points`.
>
> *Future bindings.* A Rust extension would declare its capability through Cargo features + an inventory crate or similar; a Node extension would declare via a key in `package.json`. The architectural requirement (`installation = integration`) is identical; the binding differs.

#### D2.7 — AEOS manifest at the package root

The `axiom-extension.toml` manifest declares the extension's name, version, kind, and `provides` per AEOS §5. This is the single language-agnostic descriptor every extension carries — independent of the language-ecosystem mechanism in D2.6 (which is the *binding* of the manifest's discovery contract to a particular language). Consumers can read AEOS manifests without invoking any language runtime.

### D3 — Standalone behavior contract

A standalone Axiom extension MUST install correctly + behave correctly in all of the following forms. The contract is **architectural** — what each form means is identical across language ecosystems; how each form is satisfied differs by binding.

| Form | What "correct" means (architectural) | Verification (Python example) |
|---|---|---|
| **Bare-metal** (the wrapped tool's native binary on PATH; no container) | The extension's presence is undetectable from the wrapped tool's traditional surface. Direct CLI use + native API use of the wrapped tool are unaffected. The wrapper does not shadow the wrapped tool's namespace. | `import openmc; openmc.run()` works exactly as if the wrapper were not installed. |
| **Containerized** (no native binary; only a container runtime available) | The wrapper's containerized runner pulls the official upstream image (`<tool>/<tool>:latest` by convention) and dispatches to it. | Adapter's `runner="docker"` mode invokes `openmc/openmc:latest` and parses the result. |
| **Axiom-enabled environment** | The platform's discovery mechanism finds the extension; the bonus value (live dashboard, signed receipts, federation routing per ADR-040) is available with no additional configuration. | `axi <verb>` resolves the extension via entry-point lookup; `get_adapter("openmc")` returns the adapter on first call. |
| **Mirror sync** | The primary SCM and the mirrored secondary surface carry the same tags + releases at all times. | GitHub push-mirror to an institutional GitLab. |
| **Registry resolvable** | The language-ecosystem registry returns the latest released version within minutes of the publish workflow completing. | `pip index versions axiom-ext-openmc` returns the freshly-published version. |

Failing any of these means the extension is not v1-ready. The check list goes into the standalone repo's release-blocker template.

### D4 — Builtin extensions stay simple

When the rule says "builtin," the extension lives inside the consumer's main repository per ADR-031, ships with the consumer's release cadence, and does not need its own SCM repo, secondary mirror, or ecosystem-registry release. The AEOS manifest is still required (one per builtin). Discoverability is handled by the consumer's existing extension-discovery mechanism — which does *not* need to use the language ecosystem's plug-in surface (D2.6 reserves that for cross-package discovery between independently-released artifacts).

### D5 — Migration of existing in-monorepo standalone-shape extensions

Any extension currently living as `packages/<name>/` (or equivalent in-monorepo layout) inside the platform repository, that should be standalone per D1, follows this migration:

1. Create the new repository under the consumer's domain organization on the primary SCM.
2. Seed with a clean initial commit referencing the source SHA of the extraction. History preservation is optional; the source repo's `git log` remains the authoritative pre-extraction history.
3. Configure the secondary-surface mirror.
4. Set up the new repo's CI/CD pipelines.
5. Configure the language-ecosystem registry's organization-level publisher *before* the first release tag, so the project is born inside the consumer's org.
6. Tag v0.1.0; verify all five forms of D3.
7. In the source repo, follow-up: delete the in-monorepo copy and remove any related publish-workflow logic. The platform's runtime discovery (D2.6) does not change — it will find the now-external extension once installed, identical to any third-party AEOS extension.

OpenMC (`UT-Computational-NE/axiom-ext-openmc`) is the canonical first migration. Future cases follow the same shape.

### D6 — What Axiom does for the tools it wraps (the canonical thesis)

The reason every standalone Axiom extension is worth shipping — even a "thin" one whose purpose is wrapping a single underlying tool (one physics code, one LMS, one storage backend) — is that Axiom changes what the tool *is* in four concrete ways:

  a. **Way easier to install.** Installing the package is the integration step; the platform's discovery mechanism (D2.6) handles the rest. No manifest hand-editing, no manual registration, no "and now configure your shell."
  b. **Way more flexible in form.** Bare-metal, container, SSH-to-peer, or federation-dispatched, all from the same wrapper without code changes by the consumer.
  c. **More integrated.** The wrapped tool automatically composes with the rest of the platform: provenance, federation routing, classification gates, live observability, signed receipts, RACI-managed automation.
  d. **More useful as a standalone entity.** Even with no specific Axiom-aware consumer present, the wrapper acquires compositional value the moment any Axiom env exists on the host. Default-installed Axiom agents (M-O hygiene, AXI chat, PRT publishing, D-FIB diagnostics) auto-engage with the wrapped tool — error recovery, output lifecycle, failure diagnosis — for free.

> *Axiom 'breathes life' into an otherwise lifeless digital organism. Axiom is the neural spark, nervous system, and heartbeat to the host it inhabits.*

This is a normative claim of this ADR, not a future research question. It is **why** a thin wrapper around a single tool is worth shipping as its own extension at all: in an Axiom-enabled environment, agent-managed > raw binary. The bare binary cannot offer error recovery, provenance, federation routing, or live observability — those come from the host nervous system the wrapper plugs into.

The thesis is language-independent. Whether the wrapper is a Python package, a Rust crate, a Node module, or a Go plugin, in each case it is the wires through which the platform's nervous system reaches the wrapped tool.

The thesis has three operational consequences:

1. **Every extension-developer-facing doc** should communicate this framing prominently, so authors of new extensions reach for "what does Axiom add when this composes with the platform?" before they reach for "how do I cram more features into this tool's CLI?"
2. **The discovery surface is load-bearing.** AEOS manifest + the language-ecosystem's plug-in mechanism + the four-form install matrix are not chrome — they are the wires through which the host's neural spark reaches the wrapped tool.
3. **The standalone-vs-builtin rule (D1) tilts toward standalone.** Because each wrapper becomes useful the moment any Axiom env exists — not only inside the consumer it was originally built for — keeping wrappers as their own repos amplifies their reach. A standalone extension is reachable to *every* Axiom-enabled host on the network; a builtin is reachable only to that consumer's installs. This nudge should be visible to extension authors when applying D1.

Open sub-questions still worth post-Prague design work (not blocking this ADR):

- What's the minimum manifest + plug-in shape that lets a wrapper auto-engage with each default Axiom agent? (M-O state-cleanup hooks? PRT output-lifecycle hooks? D-FIB failure-pattern declarations?)
- Which agents should auto-engage by default vs. opt-in? (Conservative: opt-in; aggressive: opt-out.)
- Does the standalone tilt motivate a future ADR that revisits D1's rule with concrete agentification surface area?

---

## Consequences

**Positive:**

- One rule covers every future extension regardless of language; no more case-by-case "where does this go?" debates.
- Standalone extensions get correct governance — consumer-domain organizations own their tooling on the SCM *and* on the language registry, with the secondary-surface mirror serving partners who can't reach the primary SCM.
- Consumer release cadences decouple cleanly. The platform doesn't have to re-release every time a downstream extension ships a fix; the extension doesn't have to wait for the platform's release window.
- The consumer's ecosystem-registry organization page (e.g., `pypi.org/org/<consumer>/` for Python consumers) becomes the single discovery surface for that domain's tooling.
- "Installation alone is the integration step" lets users adopt extensions without learning the manifest layer or manual-registration patterns.
- Adding a new language binding (Rust, Go, etc.) does not require revisiting this ADR; the binding implements D2.6 against its own ecosystem's plug-in mechanism, and everything else flows.

**Negative:**

- More repos + more CI/CD + more registry publisher configs to maintain. Each new specific-to-one-tool extension gets its own repo per the rule.
- "Where does this go?" still has edge cases — extensions that wrap *families* of tools; extensions whose generality is partial. The rule's "across ALL backends" clause helps, but the application is still judgment-call territory.
- Migrating existing in-monorepo extensions requires careful sequencing (delete source before consumers depend on the new registry package leaves users in limbo; delete after creates a brief duplicate-publish window — D5 handles this via tag-then-delete ordering).
- Mirror discipline must be operationally enforced (push hooks or scheduled sync) — a stale mirror is worse than no mirror, because consumers think they have current code.

---

## Implementation phasing

| Phase | Scope | Status |
|---|---|---|
| **P1 — OpenMC migration (Python, first canonical case)** | First standalone repo + secondary-surface mirror + registry-org publish; canonical reference for the rule | In flight (Wed-prep) |
| P2 — Practitioner companion doc | `docs/working/extension-developer-guide.md` (lands alongside this ADR) | Companion file in this PR |
| P3 — Migrate any other in-monorepo standalone-shape extensions | Audit; migrate or justify exception | Proposed |
| P4 — Agentification design task | Background; revisit post-Prague | Background only |
| P5 — Future language bindings | Rust/Go/Node bindings of D2.6 once the first non-Python extension is requested | Future |

---

## Notes

- This ADR is intentionally domain-agnostic per `feedback_axiom_domain_agnostic`. The rule applies to a domain consumer, classroom (Keplo), federation (Vega), marketplace (Vyzier) extensions equally; only the consumer-domain SCM/mirror/registry organization names differ.
- This ADR is also intentionally **language-agnostic**. Python is the canonical first instantiation because it is the language of axiom-os, the domain consumer, and the first complete extension stack. The architectural decisions hold for any language whose ecosystem can express the primitives in D2.6 + D2.7.
- This ADR does NOT change the AEOS manifest schema or the discovery interface; it codifies *where* extensions live and *how* they're released. The "what" (capability surface, AEOS conformance) is unchanged from `spec-aeos-0.1.md` / `spec-aeos-1.0.md`.
- D2.6's "install = integration, discovered identical to any third-party extension" is the *discovery* contract and still holds. **ADR-048 refines what each *brand* displays from that discovered set**: discovery is universal, visibility is brand-scoped by tier (the platform brand does not surface a domain product's extensions; a domain distribution inherits the platform base). Genuine third-party plugins remain universally visible — only portfolio siblings are scoped.
- The rule treats `packages/<name>/` (or any in-monorepo standalone-shape pattern) as a transitional state, not a target shape. After P3 the source repository's package directory is either empty or contains only legacy items pending migration.
