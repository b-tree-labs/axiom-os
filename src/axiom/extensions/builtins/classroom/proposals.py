# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LMS proposal queue (Phase 0.2) — adapter-neutral.

A proposal is a queued change to an LMS resource (page edit, new
announcement, assignment description refinement, etc.) with explicit
provenance and an instructor-approval gate. Per
`feedback_lms_agnostic_design`, this model is LMS-neutral; Canvas is
the first push target but Moodle / Blackboard / Brightspace / Google
Classroom adapters consume the same shape.

Storage layout (file-per-proposal, JSON):
    ~/.axi/coordinator/classrooms/<cid>/proposals/<proposal_id>.json

Status transitions:
    draft → approved | rejected
    approved → pushed
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ProposalStatus = str  # "draft" | "approved" | "rejected" | "pushed"

VALID_STATUSES = ("draft", "approved", "rejected", "pushed")
VALID_TARGETS = ("page", "announcement", "assignment", "module")
VALID_ACTIONS = ("create", "update")


@dataclass
class LMSProposal:
    """A queued LMS resource change awaiting instructor approval."""

    proposal_id: str
    classroom_id: str
    target: str  # one of VALID_TARGETS
    target_id: str  # existing slug/id; empty for "create"
    action: str  # one of VALID_ACTIONS
    title: str
    body: str  # HTML for pages/announcements; description text for assignments
    created_at: str
    created_by: str  # principal id (e.g. "instructor:ondrej", "chalke")
    status: ProposalStatus = "draft"
    provenance: dict[str, Any] = field(default_factory=dict)
    approved_by: str = ""
    approved_at: str = ""
    rejected_reason: str = ""
    rejected_at: str = ""
    pushed_lms_id: str = ""
    pushed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LMSProposal:
        return cls(**data)


class ProposalStore:
    """File-backed proposal queue.

    Each proposal lives in its own JSON file under ``base_dir`` keyed
    by ``proposal_id``. Idempotent reads; mutations rewrite the file.
    """

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)

    # ---- create ----------------------------------------------------------

    def create(
        self,
        *,
        classroom_id: str,
        target: str,
        target_id: str,
        action: str,
        title: str,
        body: str,
        created_by: str,
        provenance: dict[str, Any] | None = None,
    ) -> LMSProposal:
        if target not in VALID_TARGETS:
            raise ValueError(
                f"target must be one of {VALID_TARGETS}, got {target!r}"
            )
        if action not in VALID_ACTIONS:
            raise ValueError(
                f"action must be one of {VALID_ACTIONS}, got {action!r}"
            )
        if action == "update" and not target_id:
            raise ValueError("update action requires a non-empty target_id")

        proposal = LMSProposal(
            proposal_id=str(uuid.uuid4()),
            classroom_id=classroom_id,
            target=target,
            target_id=target_id,
            action=action,
            title=title,
            body=body,
            created_at=datetime.now(UTC).isoformat(),
            created_by=created_by,
            provenance=provenance or {},
        )
        self._save(proposal)
        return proposal

    # ---- read ------------------------------------------------------------

    def get(self, proposal_id: str) -> LMSProposal:
        path = self._path(proposal_id)
        if not path.exists():
            raise KeyError(f"no proposal with id {proposal_id!r}")
        return LMSProposal.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(
        self,
        *,
        classroom_id: str | None = None,
        status: ProposalStatus | None = None,
    ) -> list[LMSProposal]:
        if not self.base_dir.exists():
            return []
        out: list[LMSProposal] = []
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                p = LMSProposal.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError):
                continue
            if classroom_id is not None and p.classroom_id != classroom_id:
                continue
            if status is not None and p.status != status:
                continue
            out.append(p)
        out.sort(key=lambda p: p.created_at)
        return out

    # ---- transitions -----------------------------------------------------

    def approve(self, proposal_id: str, *, approver: str) -> LMSProposal:
        p = self.get(proposal_id)
        if p.status != "draft":
            raise ValueError(
                f"can only approve a draft; proposal {proposal_id!r} is {p.status!r}"
            )
        p.status = "approved"
        p.approved_by = approver
        p.approved_at = datetime.now(UTC).isoformat()
        self._save(p)
        return p

    def reject(
        self, proposal_id: str, *, reason: str, rejecter: str,
    ) -> LMSProposal:
        p = self.get(proposal_id)
        if p.status not in ("draft", "approved"):
            raise ValueError(
                f"cannot reject a proposal in {p.status!r} state"
            )
        p.status = "rejected"
        p.rejected_reason = reason
        p.rejected_at = datetime.now(UTC).isoformat()
        # Track the rejecter in provenance so we don't add another field
        p.provenance.setdefault("rejected_by", rejecter)
        self._save(p)
        return p

    def mark_pushed(self, proposal_id: str, *, lms_id: str) -> LMSProposal:
        p = self.get(proposal_id)
        if p.status != "approved":
            raise ValueError(
                f"can only push an approved proposal; "
                f"proposal {proposal_id!r} is {p.status!r}"
            )
        p.status = "pushed"
        p.pushed_lms_id = lms_id
        p.pushed_at = datetime.now(UTC).isoformat()
        self._save(p)
        return p

    # ---- internal --------------------------------------------------------

    def _path(self, proposal_id: str) -> Path:
        return self.base_dir / f"{proposal_id}.json"

    def _save(self, p: LMSProposal) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(p.proposal_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(p.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)
