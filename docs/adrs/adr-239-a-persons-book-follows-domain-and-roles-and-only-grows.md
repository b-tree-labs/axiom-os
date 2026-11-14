# ADR-239: A person's Book follows domain and roles, and only grows

**Status:** Proposed (2026-11-09). Earmarked; nothing is built.
**Related:** ADR-213 (upload is a composable extraction pipeline, D11 Data Book schemes), ADR-212 (Library upload filing), [Library PRD](../prds/prd-library.md)

## Context

The Library files uploads into a classification tree called a Book. Axiom ships a generic Data Book, and a product can extend it with its own scheme and display name.

Today a scheme is chosen per node. Everyone on a node sees the same tree, whatever their work. One existing product hard-codes a single persona into its Book, which does not extend to a domain with several kinds of user.

## Decision

1. A person's Book is derived from their **domain and their roles**: `<X> Book`, where the domain's scheme supplies the name and the categories.
2. A scheme declares which categories belong to which roles. A category with no declared role belongs to everyone in the domain.
3. **Adding a role adds that role's categories** to the person's Book.
4. **Removing a role leaves the Book unchanged.** Categories are never removed because a role was removed, so a person's filed work keeps its place. The person can still use those categories and upload to them.
5. Auto-categorization of a new upload uses the person's current domain and roles to choose among matching categories.
6. Categories only organize files. They are not storage locations. A Book is not a grant. Who may read a file stays with the permission system.

## Consequences

- Book contents become per-person state, recorded so a later role change can be applied as an addition.
- Hard-coded personas move to a role declaration in the scheme.
- Existing node-wide schemes remain valid: a scheme with no role declarations gives everyone the same tree.

## Open questions

- May a person hide a category from their own Book?
- Is a Book per person, or a per-person view over a shared tree?
- How does a person with several domains see their Books?
- Which role source applies: site roles, gate roles or both?
