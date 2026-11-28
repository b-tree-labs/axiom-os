# ADR-279: One MCP surface with scoped profiles

**Status:** Proposed (2026-11-24). A stub: it becomes Accepted only when its chunk lands with the proof passing.
**Related:** ADR-038, ADR-072, ADR-073; chunk [C10](https://github.com/b-tree-labs/axiom-os-private/issues/2324) of the AEOS 1.0 plan (`docs/aeos/1.0/plan.md`, private)

## Context

Besides the registry-generated MCP surface, two extensions ship standalone MCP servers, so a client sees different tools depending on which server it connected to.

## Decision (adopted Nov 24 2026, to be proven)

- **One MCP surface.** The classroom and memory standalone MCP servers fold into the one registry-generated surface. An audience that needs less gets a scoped profile (for example instructor).

## Consequences

- C10 (One MCP surface) delivers it. Scope: Fold the classroom and memory standalone MCP servers into the composed registry surface; add an instructor scoped profile.
- Retired in the same chunk: classroom/mcp_server.py, memory/mcp_server.py and memory/register_mcp.py leftovers, and every [mcp_servers.*] block in builtin manifests.

## Proof

- C10: pytest tests/mcp/test_one_surface.py: no [mcp_servers.*] block in any builtin manifest; the instructor profile lists only classroom tools; every former standalone tool is on the composed surface.
