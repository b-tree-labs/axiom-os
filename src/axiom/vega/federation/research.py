# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Call to Research — distributed research coordination protocol.

Enables any trusted node to post a research challenge, decompose it into
composable parts, collect responses from federation peers, and publish
assembled findings to the knowledge corpus.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import yaml


class CallLevel(Enum):
    FACT_RETRIEVAL = 1
    LITERATURE_SURVEY = 2
    COMPUTATIONAL = 3
    ANALYTICAL = 4
    SYNTHESIS = 5


class CallStatus(Enum):
    DRAFT = "draft"
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    ASSEMBLING = "assembling"
    PUBLISHED = "published"
    CLOSED = "closed"


class PartStatus(Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVISION_REQUESTED = "revision_requested"


@dataclass
class ResearchPart:
    part_id: str
    description: str
    part_type: str  # fact_retrieval, literature_survey, computational, analytical, synthesis, wasm_executable
    status: PartStatus = PartStatus.OPEN
    assigned_to: str = ""  # node_id
    assigned_name: str = ""  # human name
    deadline: str = ""
    response: dict | None = None
    submitted_at: str = ""

    def to_dict(self) -> dict:
        return {
            "part_id": self.part_id,
            "description": self.description,
            "part_type": self.part_type,
            "status": self.status.value,
            "assigned_to": self.assigned_to,
            "assigned_name": self.assigned_name,
            "deadline": self.deadline,
            "has_response": self.response is not None,
            "submitted_at": self.submitted_at,
        }


@dataclass
class ResearchResponse:
    part_id: str
    responder_node_id: str
    responder_name: str
    content: dict  # structured response
    provenance: list[str] = field(default_factory=list)
    submitted_at: str = ""
    accepted: bool = False

    def to_dict(self) -> dict:
        return {
            "part_id": self.part_id,
            "responder_node_id": self.responder_node_id,
            "responder_name": self.responder_name,
            "provenance": self.provenance,
            "submitted_at": self.submitted_at,
            "accepted": self.accepted,
        }


@dataclass
class CallToResearch:
    call_id: str
    title: str
    description: str
    caller_node_id: str
    caller_name: str
    level: CallLevel
    status: CallStatus = CallStatus.DRAFT
    parts: list[ResearchPart] = field(default_factory=list)
    scope: str = "consortium"
    access_tier: str = "public"
    license: str = "CC-BY-4.0"
    created_at: str = ""
    deadline: str = ""
    responses: list[ResearchResponse] = field(default_factory=list)
    synthesis: str = ""
    publication: dict | None = None
    tags: list[str] = field(default_factory=list)
    input_from: list[str] = field(default_factory=list)
    output_to: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "title": self.title,
            "description": self.description,
            "caller_node_id": self.caller_node_id,
            "caller_name": self.caller_name,
            "level": self.level.value,
            "status": self.status.value,
            "parts": [p.to_dict() for p in self.parts],
            "scope": self.scope,
            "access_tier": self.access_tier,
            "license": self.license,
            "created_at": self.created_at,
            "deadline": self.deadline,
            "response_count": len(self.responses),
            "parts_complete": sum(1 for p in self.parts if p.status == PartStatus.ACCEPTED),
            "parts_total": len(self.parts),
            "tags": self.tags,
            "input_from": self.input_from,
            "output_to": self.output_to,
            "has_synthesis": bool(self.synthesis),
            "has_publication": self.publication is not None,
        }


class ResearchService:
    """Manages Call to Research lifecycle."""

    def __init__(self, storage_dir: Path | None = None):
        self._dir = storage_dir or Path.home() / ".axi" / "research"
        self._dir.mkdir(parents=True, exist_ok=True)

    def create_call(
        self,
        title: str,
        description: str,
        caller_node_id: str,
        caller_name: str,
        level: int = 1,
        scope: str = "consortium",
        access_tier: str = "public",
        license: str = "CC-BY-4.0",
        deadline: str = "",
        tags: list[str] | None = None,
    ) -> CallToResearch:
        call = CallToResearch(
            call_id=f"ctr-{secrets.token_hex(8)}",
            title=title,
            description=description,
            caller_node_id=caller_node_id,
            caller_name=caller_name,
            level=CallLevel(level),
            scope=scope,
            access_tier=access_tier,
            license=license,
            created_at=datetime.now(UTC).isoformat(),
            deadline=deadline,
            tags=tags or [],
        )
        self._save(call)
        return call

    VALID_PART_TYPES = {
        "fact_retrieval",
        "literature_survey",
        "computational",
        "analytical",
        "synthesis",
        "wasm_executable",
    }

    def add_part(
        self,
        call_id: str,
        description: str,
        part_type: str = "fact_retrieval",
        deadline: str = "",
        operator_approved: bool = False,
    ) -> ResearchPart:
        call = self.get(call_id)
        if call is None:
            raise ValueError(f"Call not found: {call_id}")
        if part_type == "wasm_executable" and not operator_approved:
            raise ValueError(
                "WASM executable parts require operator approval (pass operator_approved=True)"
            )
        part = ResearchPart(
            part_id=f"{call_id}-p{len(call.parts) + 1}",
            description=description,
            part_type=part_type,
            deadline=deadline,
        )
        call.parts.append(part)
        self._save(call)
        return part

    def open_call(self, call_id: str) -> CallToResearch:
        """Transition from draft to open (requires human approval)."""
        call = self.get(call_id)
        if call is None:
            raise ValueError(f"Call not found: {call_id}")
        if call.status != CallStatus.DRAFT:
            raise ValueError(f"Can only open draft calls, current status: {call.status.value}")
        call.status = CallStatus.OPEN
        self._save(call)
        return call

    def claim_part(self, call_id: str, part_id: str, node_id: str, name: str) -> ResearchPart:
        call = self.get(call_id)
        if call is None:
            raise ValueError(f"Call not found: {call_id}")
        for part in call.parts:
            if part.part_id == part_id:
                if part.status not in (PartStatus.OPEN, PartStatus.REJECTED):
                    raise ValueError(
                        f"Part {part_id} is not available (status: {part.status.value})"
                    )
                part.status = PartStatus.CLAIMED
                part.assigned_to = node_id
                part.assigned_name = name
                self._save(call)
                return part
        raise ValueError(f"Part not found: {part_id}")

    def submit_response(
        self,
        call_id: str,
        part_id: str,
        content: dict,
        provenance: list[str] | None = None,
    ) -> ResearchResponse:
        call = self.get(call_id)
        if call is None:
            raise ValueError(f"Call not found: {call_id}")
        for part in call.parts:
            if part.part_id == part_id:
                if part.status not in (
                    PartStatus.CLAIMED,
                    PartStatus.IN_PROGRESS,
                    PartStatus.REVISION_REQUESTED,
                ):
                    raise ValueError(f"Part {part_id} not in submittable state")
                response = ResearchResponse(
                    part_id=part_id,
                    responder_node_id=part.assigned_to,
                    responder_name=part.assigned_name,
                    content=content,
                    provenance=provenance or [],
                    submitted_at=datetime.now(UTC).isoformat(),
                )
                part.status = PartStatus.SUBMITTED
                part.response = content
                part.submitted_at = response.submitted_at
                call.responses.append(response)
                # Update call status
                if call.status == CallStatus.OPEN:
                    call.status = CallStatus.IN_PROGRESS
                self._save(call)
                return response
        raise ValueError(f"Part not found: {part_id}")

    def accept_response(self, call_id: str, part_id: str) -> None:
        call = self.get(call_id)
        if call is None:
            raise ValueError(f"Call not found: {call_id}")
        for part in call.parts:
            if part.part_id == part_id:
                part.status = PartStatus.ACCEPTED
                for resp in call.responses:
                    if resp.part_id == part_id:
                        resp.accepted = True
                # Check if all parts complete
                if all(p.status == PartStatus.ACCEPTED for p in call.parts):
                    call.status = CallStatus.ASSEMBLING
                self._save(call)
                return
        raise ValueError(f"Part not found: {part_id}")

    def reject_response(self, call_id: str, part_id: str, reason: str = "") -> None:
        call = self.get(call_id)
        if call is None:
            raise ValueError(f"Call not found: {call_id}")
        for part in call.parts:
            if part.part_id == part_id:
                part.status = PartStatus.REJECTED
                part.assigned_to = ""
                part.assigned_name = ""
                self._save(call)
                return
        raise ValueError(f"Part not found: {part_id}")

    def request_revision(self, call_id: str, part_id: str, feedback: str = "") -> None:
        call = self.get(call_id)
        if call is None:
            raise ValueError(f"Call not found: {call_id}")
        for part in call.parts:
            if part.part_id == part_id:
                part.status = PartStatus.REVISION_REQUESTED
                self._save(call)
                return
        raise ValueError(f"Part not found: {part_id}")

    def publish_synthesis(
        self,
        call_id: str,
        synthesis: str,
        publication: dict | None = None,
    ) -> CallToResearch:
        call = self.get(call_id)
        if call is None:
            raise ValueError(f"Call not found: {call_id}")
        call.synthesis = synthesis
        call.publication = publication
        call.status = CallStatus.PUBLISHED
        self._save(call)
        return call

    def close_call(self, call_id: str) -> None:
        call = self.get(call_id)
        if call is None:
            raise ValueError(f"Call not found: {call_id}")
        call.status = CallStatus.CLOSED
        self._save(call)

    def link_calls(self, parent_call_id: str, child_call_id: str) -> None:
        """Create an explicit chain between two calls (bidirectional)."""
        parent = self.get(parent_call_id)
        if parent is None:
            raise ValueError(f"Parent call not found: {parent_call_id}")
        child = self.get(child_call_id)
        if child is None:
            raise ValueError(f"Child call not found: {child_call_id}")
        if child_call_id not in parent.output_to:
            parent.output_to.append(child_call_id)
            self._save(parent)
        if parent_call_id not in child.input_from:
            child.input_from.append(parent_call_id)
            self._save(child)

    def get_research_chain(self, call_id: str) -> list[CallToResearch]:
        """Walk the chain from root to leaf starting from *call_id*."""
        call = self.get(call_id)
        if call is None:
            raise ValueError(f"Call not found: {call_id}")

        # Walk up to root
        root = call
        visited: set[str] = set()
        while root.input_from:
            if root.call_id in visited:
                break
            visited.add(root.call_id)
            parent = self.get(root.input_from[0])
            if parent is None:
                break
            root = parent

        # Walk down from root
        chain: list[CallToResearch] = []
        queue = [root]
        seen: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current.call_id in seen:
                continue
            seen.add(current.call_id)
            chain.append(current)
            for child_id in current.output_to:
                child = self.get(child_id)
                if child is not None:
                    queue.append(child)
        return chain

    def get(self, call_id: str) -> CallToResearch | None:
        path = self._dir / f"{call_id}.yaml"
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return self._from_dict(data)

    def list_calls(
        self, status: str | None = None, level: int | None = None
    ) -> list[CallToResearch]:
        calls = []
        for path in sorted(self._dir.glob("ctr-*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            call = self._from_dict(data)
            if status and call.status.value != status:
                continue
            if level and call.level.value != level:
                continue
            calls.append(call)
        return calls

    def _save(self, call: CallToResearch) -> None:
        path = self._dir / f"{call.call_id}.yaml"
        data = {
            "call_id": call.call_id,
            "title": call.title,
            "description": call.description,
            "caller_node_id": call.caller_node_id,
            "caller_name": call.caller_name,
            "level": call.level.value,
            "status": call.status.value,
            "scope": call.scope,
            "access_tier": call.access_tier,
            "license": call.license,
            "created_at": call.created_at,
            "deadline": call.deadline,
            "tags": call.tags,
            "input_from": call.input_from,
            "output_to": call.output_to,
            "synthesis": call.synthesis,
            "publication": call.publication,
            "parts": [
                {
                    "part_id": p.part_id,
                    "description": p.description,
                    "part_type": p.part_type,
                    "status": p.status.value,
                    "assigned_to": p.assigned_to,
                    "assigned_name": p.assigned_name,
                    "deadline": p.deadline,
                    "response": p.response,
                    "submitted_at": p.submitted_at,
                }
                for p in call.parts
            ],
            "responses": [
                {
                    "part_id": r.part_id,
                    "responder_node_id": r.responder_node_id,
                    "responder_name": r.responder_name,
                    "content": r.content,
                    "provenance": r.provenance,
                    "submitted_at": r.submitted_at,
                    "accepted": r.accepted,
                }
                for r in call.responses
            ],
        }
        path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    @staticmethod
    def _from_dict(data: dict) -> CallToResearch:
        parts = [
            ResearchPart(
                part_id=p["part_id"],
                description=p["description"],
                part_type=p.get("part_type", "fact_retrieval"),
                status=PartStatus(p.get("status", "open")),
                assigned_to=p.get("assigned_to", ""),
                assigned_name=p.get("assigned_name", ""),
                deadline=p.get("deadline", ""),
                response=p.get("response"),
                submitted_at=p.get("submitted_at", ""),
            )
            for p in data.get("parts", [])
        ]
        responses = [
            ResearchResponse(
                part_id=r["part_id"],
                responder_node_id=r["responder_node_id"],
                responder_name=r["responder_name"],
                content=r.get("content", {}),
                provenance=r.get("provenance", []),
                submitted_at=r.get("submitted_at", ""),
                accepted=r.get("accepted", False),
            )
            for r in data.get("responses", [])
        ]
        return CallToResearch(
            call_id=data["call_id"],
            title=data["title"],
            description=data["description"],
            caller_node_id=data["caller_node_id"],
            caller_name=data["caller_name"],
            level=CallLevel(data["level"]),
            status=CallStatus(data.get("status", "draft")),
            parts=parts,
            responses=responses,
            scope=data.get("scope", "consortium"),
            access_tier=data.get("access_tier", "public"),
            license=data.get("license", "CC-BY-4.0"),
            created_at=data.get("created_at", ""),
            deadline=data.get("deadline", ""),
            tags=data.get("tags", []),
            input_from=data.get("input_from", []),
            output_to=data.get("output_to", []),
            synthesis=data.get("synthesis", ""),
            publication=data.get("publication"),
        )
