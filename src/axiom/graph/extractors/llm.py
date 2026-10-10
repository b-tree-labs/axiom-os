# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Stage 2: LLM-assisted entity and relationship extraction.

Uses an OpenAI-compatible LLM (e.g., Qwen on a remote VPN host) to extract
entities and relationships from document sections that deterministic
extraction cannot handle.

Operates on document SECTIONS (not chunks) — each section is sent
as a structured extraction prompt with the entity type registry.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

from axiom.graph.schema import Edge, Entity, EntityTypeRegistry

log = logging.getLogger(__name__)

_DEFAULT_PROMPT = """\
Extract entities and relationships from the following technical document section.

Entity types: {entity_types}
Relationship types: {relationship_types}

Return a JSON object with:
{{
  "entities": [
    {{"label": "EntityType", "name": "entity name", "properties": {{}}, "confidence": 0.0-1.0}}
  ],
  "relationships": [
    {{"type": "REL_TYPE", "from_name": "...", "from_label": "...", "to_name": "...", "to_label": "...", "confidence": 0.0-1.0}}
  ]
}}

Only extract what is explicitly stated or strongly implied.
Do not infer relationships that require domain expertise beyond the text.
If nothing can be extracted, return {{"entities": [], "relationships": []}}.

Document section:
---
{text}
---
"""


_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "name": {"type": "string"},
                    "properties": {"type": "object"},
                    "confidence": {"type": "number"},
                },
                "required": ["name"],
            },
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "from_name": {"type": "string"},
                    "from_label": {"type": "string"},
                    "to_name": {"type": "string"},
                    "to_label": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["from_name", "to_name"],
            },
        },
    },
    "required": ["entities", "relationships"],
}


def extract_from_section(
    text: str,
    source_path: str,
    registry: EntityTypeRegistry | None = None,
    llm_url: str | None = None,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
    gateway=None,
) -> tuple[list[Entity], list[Edge]]:
    """Extract entities and relationships from a document section using LLM.

    #86: preferred path is Gateway + structured_output (schema-validated
    tool-use). Legacy path (``AXIOM_LLM_URL`` env + urllib) is retained
    for backward-compatibility with existing setups.

    Args:
        text: Section text (NOT a chunk — full section)
        source_path: Source document path (for provenance)
        registry: Entity type registry (for prompt construction)
        llm_url/llm_model/llm_api_key: Legacy OpenAI-compatible endpoint.
        gateway: Optional :class:`axiom.infra.gateway.Gateway`. When
            provided, extraction routes through ``structured_output``;
            the env-var path is ignored.

    Returns:
        (entities, edges) tuple. Empty on any failure — never raises.
    """
    if not text.strip():
        return [], []

    if registry is None:
        registry = EntityTypeRegistry()

    # Build prompt (shared across both paths)
    entity_types = ", ".join(registry.entity_labels())
    rel_types = ", ".join(registry.relationship_types())
    prompt = _DEFAULT_PROMPT.format(
        entity_types=entity_types,
        relationship_types=rel_types,
        text=text[:4000],
    )

    # Preferred path: Gateway + structured_output
    if gateway is not None:
        return _extract_via_gateway(gateway, prompt, source_path)

    # Legacy path: urllib → OpenAI-compatible endpoint
    url = llm_url or os.environ.get("AXIOM_LLM_URL", "")
    model = llm_model or os.environ.get("AXIOM_LLM_MODEL", "")
    api_key = llm_api_key or os.environ.get("AXIOM_LLM_KEY", "")
    if not url:
        log.debug("No Gateway and no AXIOM_LLM_URL — skipping LLM extraction")
        return [], []

    try:
        response = _call_llm(url, model, api_key, prompt)
    except Exception as e:
        log.warning("LLM extraction failed for %s: %s", source_path, e)
        return [], []

    return _parse_extraction_response(response, source_path)


def _extract_via_gateway(
    gateway, prompt: str, source_path: str,
) -> tuple[list[Entity], list[Edge]]:
    """Gateway path — schema-validated extraction via structured_output."""
    from axiom.infra.structured_output import (
        SchemaValidationError,
        structured_output,
    )

    try:
        result = structured_output(
            gateway=gateway,
            schema=_EXTRACTION_SCHEMA,
            messages=[{"role": "user", "content": prompt}],
            system=(
                "You are an entity extraction assistant. Call the "
                "emit_graph_extraction tool with the entities and "
                "relationships you can derive from the section. "
                "Do not infer relationships that require domain expertise "
                "beyond the text."
            ),
            tool_name="emit_graph_extraction",
            task="extraction",
            max_tokens=2048,
        )
    except SchemaValidationError as e:
        log.warning("LLM extraction validation failed for %s: %s", source_path, e)
        return [], []
    except Exception as e:
        log.warning("LLM extraction failed for %s: %s", source_path, e)
        return [], []

    return _entities_and_edges_from_payload(result.value, source_path)


def _entities_and_edges_from_payload(
    payload: dict, source_path: str,
) -> tuple[list[Entity], list[Edge]]:
    entities: list[Entity] = []
    edges: list[Edge] = []

    for e in payload.get("entities", []) or []:
        try:
            entities.append(
                Entity(
                    label=e.get("label", "Concept"),
                    name=e.get("name", ""),
                    properties=e.get("properties", {}) or {},
                    confidence=float(e.get("confidence", 0.5)),
                    provenance="llm_extracted",
                    source_path=source_path,
                )
            )
        except (TypeError, ValueError):
            continue  # malformed confidence — skip this entity

    for r in payload.get("relationships", []) or []:
        try:
            edges.append(
                Edge(
                    rel_type=r.get("type", "REFERENCES"),
                    from_name=r.get("from_name", ""),
                    from_label=r.get("from_label", "Concept"),
                    to_name=r.get("to_name", ""),
                    to_label=r.get("to_label", "Concept"),
                    confidence=float(r.get("confidence", 0.5)),
                    provenance="llm_extracted",
                    source_chunk_id=None,
                )
            )
        except (TypeError, ValueError):
            continue

    return entities, edges


def _call_llm(url: str, model: str, api_key: str, prompt: str) -> str:
    """Call an OpenAI-compatible chat completions endpoint."""
    endpoint = f"{url.rstrip('/')}/chat/completions"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are an entity extraction assistant. Return only valid JSON.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")

    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())

    return body["choices"][0]["message"]["content"]


def _parse_extraction_response(
    response: str,
    source_path: str,
) -> tuple[list[Entity], list[Edge]]:
    """Legacy-path parser — strips markdown fence and json.loads the body."""
    json_str = response.strip()
    if json_str.startswith("```"):
        lines = json_str.split("\n")
        json_str = "\n".join(line for line in lines if not line.strip().startswith("```"))

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        log.warning("Could not parse LLM extraction response as JSON")
        return [], []

    return _entities_and_edges_from_payload(data, source_path)
