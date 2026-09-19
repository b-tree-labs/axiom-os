# SKILL: data.ingest_push

**Owner:** MCP / HTTP push front door · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active
**Last updated:** 2026-06-25

## What this skill does

The **push** counterpart of `data.ingest` (which *pulls* from a connector's
source). A client supplies items inline — `{source, items:[{item_id, content,
content_encoding, content_type, source_path, metadata}]}` — and each is routed
through the existing provenance-gated `BronzeWriter` into bronze, with optional
RAG embed. Per-item dispositions (`landed` / `quarantined` / `excluded` /
`error`) come back in `value`.

This is the MCP-reachable peer of the `POST /ingest` HTTP endpoint
(`ingest_sink/api.py`). Both share one core, `ingest_sink.core.IngestSink`
(ADR-079 §8.4.1 IngestSink endpoint, "shared core, two views"; PRD RDQ-001
push-first egress).

There is no `axi data ingest-push` CLI verb — inline bytes on a command line
are not a usable surface; the pull-side `data.ingest` covers the CLI.

## Inputs / Outputs

See the function docstring at
`axiom.extensions.builtins.data_platform.skills.ingest_push.run`. A caller may
inject a prebuilt `sink` (tests), or pass `connector` to resolve the bronze
root + provenance rules from connector config. Returns a uniform `SkillResult`.

## Safety

The provenance/disposition gate runs inside the `BronzeWriter` — unknown
sources fail safe to the configured default disposition (quarantine for an
untrusted posture). The action is wrapped in the extension's `_authz.action`
audit context. Callbacks are best-effort: a failing hook is logged and
swallowed, never breaking the landing path.
