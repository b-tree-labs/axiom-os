# ADR-031: Extension Self-Containment — Docs and Tests Co-Located with Code

**Status:** Proposed
**Date:** 2026-04-21
**Deciders:** Benjamin Booth
**Technical Story:** Repo hygiene discipline in preparation for public release and product extraction

---

## Context

As the Axiom portfolio approaches public release and product extractions proceed (Vega and Keplo extraction planned), our current repository layout has accumulated inconsistencies that will become repo-debt and adoption friction:

1. **Extension documentation is scattered.** Extension PRDs and specs live in `axiom/docs/prds/` and `axiom/docs/specs/` rather than alongside the extension code (e.g., `prd-classroom.md` lives in `axiom/docs/prds/` instead of next to the classroom extension).

2. **Extension tests are partially scattered.** Some tests live in per-extension directories; others are in `axiom/tests/`. No consistent pattern.

3. **Extraction is costly.** When an extension extracts to its own repo (Vega, Keplo), docs and tests must be manually relocated from multiple scattered directories.

4. **Discoverability suffers.** New contributors cannot find all information about an extension in one place.

5. **Vestigial docs remain.** Extracted products' PRDs and specs still live in `axiom/docs/` despite those products being standalone repos. Stale cross-references will confuse external users once the repo is public.

Public scrutiny of repo structure starts soon. Mature open-source ecosystems (Kubernetes SIGs, Apache subprojects, CNCF projects) use self-contained subproject layouts because they scale and they respect ownership boundaries. Axiom should match this standard before first impressions are formed.

---

## Decision

Every extension — built-in or external, existing or future — is structured as a self-contained unit with its own docs, tests, README, changelog, and manifest co-located with its code.

### Canonical extension layout

```
<extension-root>/
├── <extension_package>/      # Python package (importable module)
├── docs/                     # Extension-specific documentation
│   ├── prd.md
│   ├── spec.md
│   ├── working/
│   └── decisions/
├── tests/                    # Extension-specific tests
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── README.md                 # Extension overview
├── CHANGELOG.md              # Release notes (Keep-a-Changelog)
├── axiom-extension.toml      # Extension manifest
└── LICENSE                   # Only if different from repo-level
```

### What stays in `axiom/docs/` (core-only)

After migration, `axiom/docs/` contains only content about Axiom-core platform concerns:

- `adrs/` — platform-level architectural decisions
- `prds/` — core platform PRDs (agent runtime, memory, gateway, extension system, federation foundation)
- `specs/` — core platform specifications
- `working/` — portfolio strategy, cross-cutting design docs
- `papers/` — research publications
- `reference/` — external citations

### What stays in `axiom/tests/` (cross-cutting only)

After migration, `axiom/tests/` contains only cross-cutting and integration tests that span multiple extensions or exercise the core platform end-to-end. Single-extension tests move into their extension's `tests/` directory.

### Special case: Vega pre-extraction consolidation

Vega currently sprawls across `axiom/src/axiom/federation/`, `security/`, and `identity/`. Under this decision, these consolidate into a single pre-extraction staging directory:

```
axiom/src/axiom/vega/
├── federation/
├── identity/
├── trust/
├── classification/
├── docs/
├── tests/
└── README.md
```

Compatibility shims at the old import paths (`axiom.federation`, `axiom.security`, `axiom.identity`) re-export from the new locations with deprecation warnings through Axiom 0.15.x, then remove in 0.16.x.

### Applied to all current extensions

Extensions requiring migration:
- `axiom/src/axiom/extensions/builtins/classroom/` (becomes Keplo)
- `axiom/src/axiom/extensions/builtins/eve_agent/`
- `axiom/src/axiom/extensions/builtins/mo_agent/`
- `axiom/src/axiom/extensions/builtins/prt_agent/`
- `axiom/src/axiom/extensions/builtins/dfib_agent/`
- `axiom/src/axiom/extensions/builtins/neut_agent/`
- `axiom/src/axiom/extensions/builtins/connect/`
- `axiom/src/axiom/extensions/builtins/memory_cmd/`
- `axiom/src/axiom/extensions/builtins/rag/` (core — may remain differently)
- Any other built-in extensions
- Pre-extraction staging: `axiom/src/axiom/vega/`

---

## Consequences

### Positive

- **Clean extraction.** Moving an extension to its own repo becomes `git filter-repo --path <extension-root>`. No hunting for scattered docs or tests.
- **Clear ownership.** Everything related to an extension is in one directory. `git blame` attributions, GitHub CODEOWNERS rules, and contribution reviews all align to the extension boundary.
- **Discoverability.** A new contributor browsing an extension's directory sees its docs, tests, manifest, and code without cross-references.
- **Mature ecosystem alignment.** Matches Kubernetes SIG, Apache subproject, CNCF project organization patterns. Public perception benefit.
- **Independent versioning possible.** Extensions with their own CHANGELOG can be versioned independently if we choose (useful for community extensions).
- **Reduces `axiom/docs/` clutter.** Core platform docs become navigable.
- **Forcing function for ownership discipline.** When moving content to an extension directory, contributors must ask "does this belong here?" — surfaces latent cross-extension coupling.

### Negative

- **Docs fragmentation at the directory level.** A unified documentation website requires generation tooling (MkDocs with monorepo plugin, or Sphinx with sub-project includes). Cannot browse all docs in one folder.
- **Cross-references use repo-relative paths.** Links between extension docs traverse more directories.
- **README boilerplate per extension.** Mitigate via template; pay maintenance cost.
- **Migration cost.** Moving existing scattered content requires care with git history preservation (use `git mv` to retain history).
- **Breaking change for Vega consolidation.** Code import paths change (`axiom.federation` → `axiom.vega.federation`). Mitigated by compatibility shims but still a deprecation cycle.

### Neutral

- **New contributors need to learn the layout.** Mitigate via template and the spec (spec-extension-layout.md, following this ADR).
- **CI must enforce layout.** New lint check validates every extension directory conforms.

---

## Implementation Plan

### Phase 1: Establish the standard (Week 1)

1. Create `docs/specs/spec-extension-layout.md` documenting the canonical layout in detail (naming conventions, required files, optional files, examples).
2. Create a cookiecutter template for new extensions (`tools/extension-template/`).
3. Add `axi ext lint` extension layout check that verifies every extension has the canonical structure.
4. Add CI gate enforcing `axi ext lint` passes for all extensions.

### Phase 2: Migrate existing extensions (Weeks 1-2)

Execute migrations as small, reviewable PRs — one per extension. For each:

1. Create `<extension>/docs/` and `<extension>/tests/` subdirectories
2. `git mv` related content from `axiom/docs/prds/` and `axiom/docs/specs/` into `<extension>/docs/`
3. `git mv` related tests from `axiom/tests/` into `<extension>/tests/`
4. Rename files to canonical names (`prd-classroom.md` → `docs/prd.md`)
5. Update any internal links to new paths
6. Add `README.md` and `CHANGELOG.md` using template
7. Verify `axi ext lint` passes

Order of migration (least risky first):
1. `dfib_agent/` (small, self-contained)
2. `connect/`
3. `memory_cmd/`
4. `mo_agent/`
5. `prt_agent/`
6. `eve_agent/` (larger, more interconnected)
7. `neut_agent/`
8. `classroom/` (largest; coordinated with Keplo extraction planning)

### Phase 3: Vega pre-extraction consolidation (Week 2-3)

1. Create `axiom/src/axiom/vega/` directory
2. `git mv axiom/src/axiom/federation/ axiom/src/axiom/vega/federation/`
3. `git mv axiom/src/axiom/identity/ axiom/src/axiom/vega/identity/`
4. `git mv axiom/src/axiom/security/ axiom/src/axiom/vega/trust/` (rename for clarity)
5. Create `axiom/src/axiom/vega/docs/` with prd.md, spec.md, working/plan-extraction.md (moved from `axiom/docs/`)
6. Create `axiom/src/axiom/vega/tests/`
7. Add compatibility shims at old import paths:
   ```python
   # axiom/src/axiom/federation/__init__.py
   import warnings
   warnings.warn(
       "axiom.federation is moving to axiom.vega.federation; "
       "update imports. Old path removed in Axiom 0.16.",
       DeprecationWarning,
       stacklevel=2,
   )
   from axiom.vega.federation import *  # noqa
   ```
8. Update internal Axiom imports to use new paths
9. Add migration note to Axiom 0.14.0 release notes

### Phase 4: Clean up vestigial references (Week 2)

1. Delete a standalone product's vestigial PRD/spec from `axiom/docs/` (they belong in that product's own repo)
2. Move Vega docs from `axiom/docs/` to `axiom/src/axiom/vega/docs/`
3. Update any cross-repo links
4. Add `axiom/docs/MIGRATED.md` explaining the new layout for returning contributors

### Phase 5: Unified docs generation (Week 3)

1. Set up MkDocs Material at repo root with the monorepo plugin (or Sphinx with subproject includes)
2. Configure to aggregate from `docs/` and every `src/axiom/extensions/builtins/*/docs/`
3. Build site published to GitHub Pages on main branch
4. Navigation organized by platform → extensions → reference

---

## Compliance and Enforcement

After this ADR accepts:

- **New extensions must conform** from first commit. Template provided; `axi ext lint` enforces.
- **Pre-existing extensions migrate** per Phase 2 schedule.
- **No extension-specific docs may be added to `axiom/docs/`** — they belong in the extension directory.
- **CI fails** on extensions missing required files (docs/, tests/, README, CHANGELOG, manifest).

---

## Rejected Alternatives

### A1: Keep current scattered layout

Rejected. Accumulates technical debt; makes extraction expensive; poor discoverability; doesn't match mature ecosystem patterns.

### A2: Only co-locate docs, keep tests centralized

Rejected. Half-measure. Tests belong with their extension for the same reasons as docs (ownership, discoverability, extraction cleanliness).

### A3: Full flat layout (every extension at top level, no `extensions/builtins/` grouping)

Rejected. Loses the clear distinction between built-in and external extensions, and between platform code and extension code. The current grouping aids navigation.

### A4: Defer Vega consolidation until extraction time

Rejected. Pays the migration cost twice (once now for docs/tests, once later for code), and leaves a known-broken layout in the repo during the public release period when impressions are formed.

---

## References

- [Kubernetes SIG structure](https://github.com/kubernetes/community) — mature example of co-located subproject organization
- [Apache project layout conventions](https://www.apache.org/foundation/how-it-works.html#pmc)
- [Keep a Changelog](https://keepachangelog.com/) — CHANGELOG.md format standard
- [Vega extraction plan](../working/plan-vega-extraction.md) — execution coordination
- [Brand and product strategy](../working/brand-product-strategy.md) — portfolio context
