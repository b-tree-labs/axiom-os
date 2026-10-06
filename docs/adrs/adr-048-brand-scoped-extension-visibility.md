# ADR-048: Brand-Scoped Extension Visibility — The Distribution Model

**Status:** Proposed (2026-05-28)
**Supersedes:** none
**Refines:** ADR-044 (extension distribution model — D2.6 made runtime discovery brand-agnostic; this ADR scopes *visibility* by brand without changing *discovery* or the manifest schema)
**Related:** ADR-031 (extension self-containment), ADR-032 (standards positioning), ADR-046 (RIVET/TIDY boundary — agents are platform-tier), **ADR-047 (availability-aware CLI dispatcher — the sibling §2.10 surfacing rule: it gates on *capability availability* (unmet `requires`), this one gates on *brand/portfolio tier*; both hide presence, never invocation)**.
**Specs:** `spec-aeos-0.1.md` (§5 capability kinds, discovery contract).
**Memory ground:** `feedback_portfolio_products_separate_from_axiom`, `project_axiom_best_goal_locked`.

---

## Context

A single venv can hold the Axiom platform package (`axiom-os-lm`) *and* one or
more domain packages built on it (`example-consumer`, future `keplo`, etc.). Each
package installs a branded console-script: `axi` (Axiom), `neut` (the domain consumer).
Today both entry points run the same runtime and call the same
`discover_extensions()`, which scans **every** installed package for an
`extensions/builtins/` directory and surfaces what it finds (ADR-044 §D2.6:
*"identical to any third-party AEOS extension"*).

The observable result (2026-05-28):

- `axi agents status` lists `model_corral` — a consumer-extension extension (*"model
  registry"*, `owner = "an-institution"`). The **platform** brand is surfacing
  a **domain** extension.
- `neut agents status` lists the platform agents (TIDY/RIVET/TRIAGE) but labels
  the dispatcher `Axiom-Background-Service` and warns it couldn't find a
  consumer-specific Background Service wrapper.

Two different surprises, and they are not symmetric. This is the unresolved
tension between two existing positions in the repo:

- `docs/working/brand-product-strategy.md` wants **brand-scoped surfaces**:
  *"a university doesn't want a domain product's features, a domain lab doesn't want
  classroom features."*
- ADR-044 §D2.6 made **discovery brand-agnostic**: install = integration,
  everything installed is found.

Both are right within their frame; nothing reconciled them. This ADR does.

### The precedent: how Linux handles base-vs-distribution

Linux already solved this, and its answer is unambiguous:

| Linux | What it does | Maps to |
|---|---|---|
| coreutils / systemd | **Neutral, stable names** on every distro. No `ubuntu-ls`, no `fedora-systemctl`. The base is never re-namespaced. | Axiom platform agents (TIDY/RIVET/TRIAGE/PRESS/AXI) + the Background Service dispatcher |
| `apt` / `dnf` / `pacman` | The distro brands the layer **it owns** (package/system management) — and *only* that layer, not the borrowed coreutils. | A domain product's **own** extensions (e.g. a consumer's `model`, experiment-manager) |
| `/etc/os-release` (`NAME=Ubuntu`) | Brand **identity as data**, read by neutral tools (`lsb_release`). Not a prefix on every binary. | The active `BrandingConfig` (banner, `product_name`) |

The Linux rule, stated generally:

> **Brand what you own. Keep neutral, stable names for what you borrow.
> Surface the distribution identity as data, not by renaming the base.**

White-labeling the *base* (hiding that Linux/systemd is underneath, renaming
the borrowed tools) is the **appliance / OEM** pattern — a legitimate but
*different* product posture with real costs (loss of ecosystem familiarity,
full-stack support burden). It is not the distribution pattern.

---

## Decision

### D1 — A domain consumer (and every domain product) is a *distribution*, not an appliance

The default posture across the portfolio is the **distribution model**:

- **Platform tools keep neutral, stable names.** Axiom's agents and the
  Background Service are shared infrastructure (coreutils/systemd). They are not
  re-branded per consumer. A domain product does not ship a re-skinned copy of
  TIDY; it *inherits* TIDY.
- **A brand owns only its own domain layer.** A domain consumer brands its own domain /
  experiment extensions (the `apt` it owns), not the platform agents it borrows.
- **Identity is data.** The active brand changes the banner, product name, and
  its own commands — not the names of borrowed platform tools.

The **appliance model** (single sealed brand, base hidden, the platform tools
removed from the surface entirely) remains available as an explicit *deployment
posture* for OEM/sealed customers, but it is **opt-in** and out of scope for
this ADR's implementation. Crucially, appliance mode means shipping **one**
brand surface — not co-installing `axi` alongside a re-skinned `neut`. The
current mess comes precisely from being half-both.

### D2 — The tier-asymmetry visibility rule

Extensions form tiers. A brand surfaces its own tier and every tier **below**
it — never a tier **above**, never a **sibling**.

```
        ┌─────────────────────────────┐
        │  domain layer (a product)    │   neut shows: platform + example-consumer
        │   example-consumer | keplo | …│   (its own + below); NOT keplo (sibling)
        ├─────────────────────────────┤
        │  platform base (Axiom)       │   axi shows: platform + 3rd-party
        │   TIDY RIVET TRIAGE PRESS …  │   plugins; NOT example-consumer/keplo (above)
        └─────────────────────────────┘
```

This resolves the two surprises **differently**, which is the crux:

| Observed | Verdict | Why |
|---|---|---|
| `axi` shows `model_corral` | **Bug** | Platform (below) surfacing a domain product (above). Also a positioning failure — an Axiom customer must not see "physics model registry." |
| `neut` shows TIDY/RIVET | **Correct** | A distribution *is* the full stack; inheriting platform agents is the value of building on the substrate. |
| `neut` prints `Axiom-Background-Service` | **Bug (naming, not visibility)** | systemd isn't "Ubuntu-systemd." The dispatcher is borrowed base; it must carry a stable, brand-neutral functional name, not the platform *company* brand and not a per-consumer rebrand. |

### D2.5 — Surfacing, not power (the load-bearing constraint)

Brand-scoping is a **surfacing rule under AEOS 1.0 §2.10 ("tier governs
presence, not power")**: it gates what a brand *lists/suggests*, never what is
*invocable*. Discovery stays universal — every installed extension is loaded and
remains reachable when explicitly addressed (`axi model …` still runs even
though `axi` doesn't *list* `model_corral`). The exact OSS precedent is the
freedesktop `.desktop` `OnlyShowIn` / `NotShowIn` / `NoDisplay` keys: an
installed, fully-launchable application that a given desktop environment chooses
not to show in its menu. Brand = desktop environment; the extension is on "PATH"
everywhere, shown in the menu only under the brands that claim it.

Consequence for implementation: the filter lives at the **listing/surfacing**
layer (`ext list`, `agents status`, menus, completion — via
`surfaced_extensions()`), **never** at the discovery/registration layer. A
filter that drops extensions from discovery would gate invocation, violating
§2.10.

### D3 — The visibility predicate (portfolio-sibling exclusion)

An installed-package extension from package `P` is **surfaced** (listed) under
the active brand iff — it remains discovered + invocable regardless:

```
P is not a registered portfolio member
  OR  P == active_brand_package
  OR  P == platform_base_package        # axiom-os-lm, always inherited
```

Equivalently: **exclude `P` iff `P` is a portfolio-member package that is
neither the active brand nor the platform base.**

- `axi` (active = `axiom-os-lm`): excludes `example-consumer`, `keplo`, … → `model_corral` hidden. Third-party (non-member) plugins kept. ✅
- `neut` (active = `example-consumer`): excludes `keplo` but keeps the Axiom base and `example-consumer`'s own. ✅

The signal is the existing **`axiom.portfolio_member` entry-points group**
(`discover_portfolio_members()`): every portfolio package self-declares, so the
platform never needs to know about future products. This preserves the ADR-044
§D2.6 contract for genuine third-party plugins (the Vyzier marketplace case):
plugins are **not** portfolio members, so they remain visible under every brand.
Only *portfolio siblings/distributions* are scoped.

This predicate is applied by `surfaced_extensions()` at **listing** time, over
the universal result of `discover_extensions()` — discovery and invocation are
never touched (D2.5). Platform builtins and project-/user-level extensions have
no portfolio-sibling source package, so they are always surfaced — the base and
the operator's own extensions are never hidden.

### D4 — Activation requires self-declaration

The filter is inert for a portfolio package until that package declares the
`axiom.portfolio_member` entry point. A domain consumer (and every future product) MUST
add it to `pyproject.toml`:

```toml
[project.entry-points."axiom.portfolio_member"]
example-consumer = "example_consumer.portfolio:_portfolio_metadata"
```

returning `{package_name, product_name, wrapper_binary}`. Until a sibling
declares itself, it is treated as a third-party plugin (visible) — a safe
default that never *hides* something unexpectedly; it only *fails to hide*.

### D5 — Neutral dispatcher naming (follow-up, scoped by this ADR)

The Background Service is borrowed base infrastructure (systemd, not
`ubuntu-systemd`). Its current per-brand console-script name
(`Axiom-Background-Service` / `Consumer-Background-Service`) is the source of
the `neut` fallback warning. The target is a **single stable, brand-neutral
functional name** for the dispatcher, with the brand surfaced only in
human-facing banners. This is a naming/console-script migration tracked as a
follow-up to this ADR; it does not block D2–D3.

---

## Consequences

**Positive**

- `axi` presents a clean, domain-agnostic surface — consistent with the locked
  Axiom positioning (`project_axiom_best_goal_locked`) and the brand-product
  strategy. No domain features leak into the platform brand.
- The rule is self-extending: products self-declare; the platform never hard-codes
  a product list (`feedback_portfolio_products_separate_from_axiom`).
- Third-party plugins (Vyzier) are unaffected — the open extension surface stays open.
- Linux-grounded mental model: "platform = coreutils, brand = apt, identity = os-release"
  is teachable and matches what engineers already know.

**Negative**

- A portfolio sibling that hasn't yet declared `axiom.portfolio_member` won't be
  scoped (visible under `axi` until it adopts the entry point). Mitigated: this
  is the safe failure direction (over-show, never wrongly-hide), and D4 makes
  adoption a one-liner.
- "Portfolio sibling vs third-party plugin" is now a load-bearing distinction.
  An extension author who wants scoping must self-declare; one who wants
  marketplace ubiquity must not. This is a deliberate, documented choice, not an
  accident.
- The dispatcher-naming migration (D5) touches console-scripts across packages
  and is deferred — until it lands, `neut` still prints the fallback warning.

---

## Notes

- This ADR changes neither the AEOS manifest schema nor the discovery
  *interface* (ADR-044 §D2.6 still holds: install = integration, everything is
  *discovered*). It scopes what each **brand** *displays* from the discovered
  set. Discovery is universal; visibility is branded.
- Domain-agnostic: the rule names no
  specific product. The domain consumer is the first instance; Keplo/Vega/Vyzier follow
  identically.
