# Library PRD

**Status:** Started 2026-11-09 to hold requirements that have no other home. The upload pipeline is specified in `src/axiom/extensions/builtins/library/docs/spec-upload-pipeline.md` and ADR-213.

## Terms

The **Library** is the rail item. A **Book** is its classification tree: the Data Book generically, and a product's own named Book when a product extends it. A **Collection** is a place in that tree.

## Requirement LIB-R1: a Book shaped by domain and roles

Earmarked, not scheduled. Decision: [ADR-239](../adrs/adr-239-a-persons-book-follows-domain-and-roles-and-only-grows.md).

| ID | Requirement |
|---|---|
| LIB-R1.1 | A person's Book is derived from their domain and roles. |
| LIB-R1.2 | A domain's scheme declares which categories belong to which roles. A category with no declared role belongs to everyone in the domain. |
| LIB-R1.3 | Adding a role adds that role's categories to the person's Book. |
| LIB-R1.4 | Removing a role leaves the person's Book unchanged. They can still use those categories and upload to them. |
| LIB-R1.5 | Auto-categorization considers the person's current domain and roles. |
| LIB-R1.6 | Categories only organize files; they are not storage locations. A Book does not grant access. The permission system decides who may read a file. |

Acceptance, when scheduled: add a role and its categories appear; remove the role, no category disappears and an upload to that category still works; a scheme with no role declarations behaves as today.
