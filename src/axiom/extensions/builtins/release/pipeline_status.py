# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PipelineStatus — the shared CI status record for RIVET.

Owned here (rather than in ``ci_monitor``) so both the provider layer and
the monitor can import it without a circular dependency. ``ci_monitor``
re-exports it for back-compat with existing callers/tests.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PipelineStatus:
    repo: str
    provider: str  # "github", "gitlab", "gitea", ...
    ref: str
    status: str  # "success", "failed", "running", "pending", ...
    url: str = ""
    failure_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "provider": self.provider,
            "ref": self.ref,
            "status": self.status,
            "url": self.url,
            "failure_reason": self.failure_reason,
        }


__all__ = ["PipelineStatus"]
