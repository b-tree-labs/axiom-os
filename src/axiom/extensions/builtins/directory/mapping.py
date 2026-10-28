# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Group → role mapping — ADR-103 decision 8.

Which group means ``operator`` is a *deployment* fact, not a code fact, so this
is external, hot-editable config in the manner of ADR-094's tier rules: an
ordered rule set, first match wins, conservative default. Re-mapping a group is
a config edit, never a code change or a deploy.

The default is empty on purpose. A group nobody has mapped must confer no
authority — the failure mode of a permissive default is silent over-grant, which
is the one failure this whole seam exists to prevent.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class GroupRoleMap:
    rules: Sequence[Mapping[str, str]] = field(default_factory=tuple)
    default: tuple[str, ...] = ()

    @classmethod
    def permissive(cls) -> GroupRoleMap:
        """Identity mapping: every group is its own role.

        For tests and for deployments that have not yet authored a mapping —
        never a production default, because it grants whatever the directory
        happens to return.
        """
        return cls(rules=({"group": "*", "role": "*"},), default=())

    @classmethod
    def from_file(cls, path: str | Path) -> GroupRoleMap:
        doc = json.loads(Path(path).read_text())
        return cls(
            rules=tuple(doc.get("rules", ())),
            default=tuple(doc.get("default", ())),
        )

    def _role_for(self, group: str) -> str | None:
        for rule in self.rules:
            pattern = rule.get("group", "")
            if fnmatch.fnmatchcase(group, pattern):
                role = rule.get("role", "")
                return group if role == "*" else role
        return None

    def roles_for(self, groups: Iterable[str]) -> tuple[str, ...]:
        """Map groups to roles: first matching rule per group, deduped, ordered.

        Order is first-seen rather than sorted so a reader can trace a role back
        to the group that produced it.
        """
        out: list[str] = []
        for g in groups:
            role = self._role_for(g)
            if role and role not in out:
                out.append(role)
        if not out:
            return tuple(self.default)
        return tuple(out)


__all__ = ["GroupRoleMap"]
