# Glossary System Specification

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-03-20
**Layer:** Axiom core

---

## Purpose

Axiom operates at the intersection of operations engineering, software
engineering, machine learning, and regulatory compliance. Each domain brings
its own vocabulary, and the same word often means different things depending
on context.

The glossary system provides:

1. **A standard format** (`glossary.toml`) for defining terms at any layer
   of the platform — Axiom core, Axiom domain, or individual extension.
2. **A roll-up mechanism** that merges glossaries across layers into a unified
   view, with higher-specificity layers able to extend (not replace) lower-layer
   definitions.
3. **A CLI interface** (`axiom glossary`) for querying terms interactively.
4. **A notation convention** for specs and PRDs that links terms to their
   definitions without duplicating them.

---

## Glossary Layers

Glossaries are organized in three layers, matching the platform architecture:

```
Axiom core          docs/glossary-axiom.toml
                         ↓ extended by
Axiom domain    docs/glossary-domain.toml
                         ↓ extended by
Extension-local     src/axiom/extensions/builtins/{ext}/glossary.toml
```

**Resolution order:** Extension > Axiom domain > Axiom core.

When a term appears at multiple layers, the higher layer's `definition` is
shown by default. The lower-layer definition is always accessible via
`axiom glossary <term> --all-layers`. Terms at higher layers that carry
`extends = true` append to (rather than replace) the Axiom definition.

---

## `glossary.toml` Format

```toml
# Each [[terms]] entry defines one term.

[[terms]]
id          = "tier"                    # Unique identifier. snake_case. Used for cross-refs.
layer       = "axiom"                   # "axiom" | "domain" | "extension"
term        = "Tier"                    # Display name (title case).
definition  = """
An access sensitivity level that determines which physical store holds
content of that tier, whether cloud embedding is permitted, and what
processing boundary applies. Tier names and properties are defined by
the domain configuration — Axiom assigns none by default.
"""
properties  = ["cloud_embedding_allowed", "requires_isolated_store",
               "processing_boundary"]  # Optional: list of config properties
see_also    = ["scope", "isolated_store", "domain_configuration"]
example     = """
In the Axiom domain-specific domain configuration, three tiers are defined:
  public           (cloud embedding allowed, local store)
  restricted       (no cloud, Private-Server isolated store)
  export_controlled (no cloud, PrivateCloud isolated store)

The third tier is Axiom's 'classified' archetype configured with the
domain-specific-domain name 'export_controlled'.
"""
extends     = false                     # true = append to lower-layer definition
                                        # false = stand-alone at this layer
```

### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique across all layers. snake_case. |
| `layer` | enum | `"axiom"` \| `"domain"` \| `"extension"` |
| `term` | string | Human-readable display name |
| `definition` | string | Full definition. Plain prose, no markdown headers. |

### Optional fields

| Field | Type | Description |
|-------|------|-------------|
| `properties` | list[string] | Config properties or schema fields for this concept |
| `see_also` | list[string] | `id` values of related terms |
| `example` | string | Worked example, typically showing domain configuration |
| `extends` | bool | If true, appends to lower-layer definition rather than replacing it. Default false. |
| `deprecated` | bool | Mark a term as superseded. `deprecated_by` field names the replacement. |
| `deprecated_by` | string | `id` of the replacement term |

---

## Discovery

Glossary files are discovered at startup in the same order as extensions:

1. `docs/glossary-axiom.toml` — Axiom core (shipped with the package)
2. `docs/glossary-domain.toml` — Domain extension layer
3. `src/axiom/extensions/builtins/*/glossary.toml` — Builtin extensions
4. `.axi/extensions/*/glossary.toml` — Project-local extensions
5. `~/.axi/extensions/*/glossary.toml` — User-global extensions

---

## CLI: `axiom glossary`

```
axiom glossary                          List all known terms (rolled-up view)
axiom glossary <term>                   Show definition of a term
axiom glossary <term> --all-layers      Show definition at every layer
axiom glossary --layer axiom            Show only Axiom core terms
axiom glossary --layer domain           Show only Axiom domain terms
axiom glossary --search <query>         Full-text search across all definitions
axiom glossary --export markdown        Render all terms as Markdown
```

---

## Notation in Specs and PRDs

Specs reference glossary terms using a standard callout notation:

> ⟦**Tier**⟧ — An access sensitivity level... (see `axiom glossary tier`)

For inline references, use backtick-term: `` `tier` `` links to the glossary
entry in rendered documentation. For first use of a term in a spec, expand
it inline; subsequent uses may use the term without annotation.

Specs should include a **Terms Used** section near the top listing the glossary
`id` values relied upon, so readers can resolve unfamiliar terms before reading:

```markdown
**Terms used in this spec:**
`tier` · `scope` · `domain_pack` · `bundle` · `isolated_store` ·
`knowledge_maturity` · `promotion_policy` · `retrieval_log`
(see `axiom glossary <term>` or docs/glossary-axiom.toml)
```

---

## Tier Naming Convention

Axiom core recognises three tier archetypes by their access properties.
Tier names are always domain-assigned — Axiom defines the properties, not
the names:

| Axiom archetype | Properties | Axiom name |
|-----------------|------------|----------------|
| `public` | cloud_embedding_allowed = true | `public` |
| `restricted` | cloud_embedding_allowed = false, isolated store required | `restricted` |
| `classified` | cloud_embedding_allowed = false, isolated store required, processing boundary enforced | `export_controlled` |

The Axiom glossary documents the `tier` concept and the three archetypes.
The Axiom domain glossary owns `export_controlled` and `restricted_tier`
as domain-specific instantiations.

When writing domain-agnostic Axiom documentation, use the archetype names
(`public · restricted · classified`). When writing Axiom-specific
documentation, use the configured names (`public · restricted · export_controlled`).

---

## Relationship to `docs/glossary.md`

`docs/glossary.md` is the Axiom human-readable glossary — narrative prose
organized by topic, written for contributors. It remains the primary reading
experience for humans.

`glossary.toml` files are the machine-readable source of truth — structured,
queryable, and rollable-up across layers. Over time, `docs/glossary.md` will
be generated from the TOML files via `axiom glossary --export markdown`.

Until generation is implemented, both files are maintained manually and should
be kept consistent.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
