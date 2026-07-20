# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Cluster-4 absorb adapter SKELETON — cloud account-bound memory APIs
(ADR-087 D8; harness-memory survey §4).

P2 ships this cluster as a **credential-seamed skeleton only** (locked
knob): the adapter interface exists, the per-vendor record mappers
exist, and everything is proven against fakes — **no live calls, no
HTTP client, no credential reads**. The seam is
:class:`CloudMemoryClient`: anything that can list memory records.
Real transports for the round-trippable vendors (survey: Devin
Knowledge has full REST CRUD; Amp threads expose an OpenAPI; Letta
Cloud has REST + export) are follow-up work injected through the seam
— this module never constructs one.

Without an injected client, ``scan()`` degrades to a skip record
naming the credential the future transport would need (e.g.
``DEVIN_API_KEY``) — visible, auditable on import, and inert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

from axiom.memory.fragment import SourceOrigin

from .base import AbsorbScan, FragmentCandidate, SkippedSource


@runtime_checkable
class CloudMemoryClient(Protocol):
    """The credential seam: a source of vendor memory records.

    Implementations own transport + auth entirely. The adapter never
    sees a credential — it sees records.
    """

    def list_memories(self) -> Iterable[dict]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class CloudAPIAdapter:
    """Generic cluster-4 engine: injected client + per-vendor mapper.

    ``credential_env`` names the environment variable a real transport
    would authenticate with; it is only ever *named* in the degraded
    skip record, never read here.
    """

    harness: str
    account: str
    client: CloudMemoryClient | None
    map_record: Callable[[dict], dict | None]
    credential_env: str

    def scan(self) -> AbsorbScan:
        scan = AbsorbScan()
        if self.client is None:
            scan.skipped.append(SkippedSource(
                source=self.harness,
                reason=(
                    f"credentials_required: inject a CloudMemoryClient "
                    f"(a real transport would authenticate via "
                    f"{self.credential_env}); no live calls are made "
                    f"without one"
                ),
            ))
            return scan
        try:
            records = list(self.client.list_memories())
        except Exception as exc:  # transport/auth failure → degrade
            scan.skipped.append(SkippedSource(
                source=self.harness, reason=f"client_error: {exc}",
            ))
            return scan
        for record in records:
            try:
                mapped = self.map_record(record)
            except (KeyError, TypeError, ValueError) as exc:
                scan.skipped.append(SkippedSource(
                    source=self.harness, reason=f"record_invalid: {exc}",
                ))
                continue
            if mapped is None:
                scan.skipped.append(SkippedSource(
                    source=self.harness,
                    reason=f"record_invalid: unusable record {record!r:.120}",
                ))
                continue
            scan.candidates.append(FragmentCandidate(
                content=mapped["content"],
                cognitive_type=mapped.get("cognitive_type", "semantic"),
                origin=SourceOrigin(
                    harness=self.harness,
                    account=self.account,
                    source_ref=mapped["source_ref"],
                    imported_at=_now(),
                ),
            ))
        return scan


# ---------------------------------------------------------------------------
# Per-vendor record mappers + factories (fake-backed in P2)
# ---------------------------------------------------------------------------


def _text_of(record: dict, *keys: str) -> str:
    for key in keys:
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def devin_knowledge_adapter(
    *, account: str, client: CloudMemoryClient | None = None
) -> CloudAPIAdapter:
    """Devin Knowledge — the round-trippable exception (full REST CRUD)."""

    def _map(record: dict) -> dict | None:
        rid = record.get("id")
        text = _text_of(record, "body", "content", "text")
        if not rid or not text:
            return None
        content: dict[str, Any] = {
            "summary": _text_of(record, "name", "title") or text.splitlines()[0],
            "text": text,
            "layer": "auto_memory",
            "fact_kind": "devin_knowledge",
        }
        created = record.get("created_at")
        if isinstance(created, str) and created:
            content["event_time"] = created
        return {"content": content, "source_ref": f"knowledge/{rid}"}

    return CloudAPIAdapter(
        harness="devin",
        account=account,
        client=client,
        map_record=_map,
        credential_env="DEVIN_API_KEY",
    )


def amp_threads_adapter(
    *, account: str, client: CloudMemoryClient | None = None
) -> CloudAPIAdapter:
    """Amp threads — cloud-searchable threads behind an OpenAPI."""

    def _map(record: dict) -> dict | None:
        rid = record.get("id")
        text = _text_of(record, "summary", "text", "title")
        if not rid or not text:
            return None
        content: dict[str, Any] = {
            "summary": _text_of(record, "title") or text.splitlines()[0],
            "text": text,
            "layer": "auto_memory",
            "fact_kind": "amp_thread",
        }
        created = record.get("created_at")
        if isinstance(created, str) and created:
            content["event_time"] = created
        return {"content": content, "source_ref": f"threads/{rid}"}

    return CloudAPIAdapter(
        harness="amp",
        account=account,
        client=client,
        map_record=_map,
        credential_env="AMP_API_KEY",
    )


def letta_cloud_adapter(
    *, account: str, client: CloudMemoryClient | None = None
) -> CloudAPIAdapter:
    """Letta Cloud — REST + export; same passage shape as self-hosted."""

    def _map(record: dict) -> dict | None:
        rid = record.get("id")
        text = _text_of(record, "text")
        if not rid or not text:
            return None
        content: dict[str, Any] = {
            "summary": text.splitlines()[0][:120],
            "text": text,
            "layer": "auto_memory",
            "fact_kind": "letta_passage",
        }
        if record.get("agent_id"):
            content["agent_id"] = record["agent_id"]
        created = record.get("created_at")
        if isinstance(created, str) and created:
            content["event_time"] = created
        meta = record.get("metadata")
        if isinstance(meta, dict):
            content["metadata"] = meta
        return {"content": content, "source_ref": f"passages/{rid}"}

    return CloudAPIAdapter(
        harness="letta-cloud",
        account=account,
        client=client,
        map_record=_map,
        credential_env="LETTA_API_KEY",
    )


__all__ = [
    "CloudAPIAdapter",
    "CloudMemoryClient",
    "amp_threads_adapter",
    "devin_knowledge_adapter",
    "letta_cloud_adapter",
]
