# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Release planning — stage features across releases for progressive rollout.

Manages a release roadmap: which features ship in which version,
auto-generates release notes from staged features + git history,
and tracks announcement status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class StagedFeature:
    name: str
    description: str
    tier: int  # progressive disclosure tier
    status: str = "staged"  # staged, shipped, announced
    cli_commands: list[str] = field(default_factory=list)  # affected commands
    test_count: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "tier": self.tier,
            "status": self.status,
            "cli_commands": self.cli_commands,
            "test_count": self.test_count,
        }


@dataclass
class ReleaseMilestone:
    version: str
    codename: str = ""
    target_date: str = ""
    status: str = "planned"  # planned, ready, tagged, announced
    features: list[StagedFeature] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "codename": self.codename,
            "target_date": self.target_date,
            "status": self.status,
            "features": [f.to_dict() for f in self.features],
            "feature_count": len(self.features),
            "total_tests": sum(f.test_count for f in self.features),
            "notes": self.notes,
        }


class ReleasePlanService:
    """Manages the release roadmap."""

    def __init__(self, plan_path: Path | None = None):
        self._path = plan_path or Path.home() / ".axi" / "release-plan.yaml"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def add_milestone(
        self, version: str, codename: str = "", target_date: str = ""
    ) -> ReleaseMilestone:
        plan = self._load()
        # Check for duplicate
        for m in plan:
            if m.version == version:
                raise ValueError(f"Milestone {version} already exists")
        milestone = ReleaseMilestone(
            version=version,
            codename=codename,
            target_date=target_date,
        )
        plan.append(milestone)
        self._save(plan)
        return milestone

    def stage_feature(
        self,
        version: str,
        name: str,
        description: str,
        tier: int = 0,
        cli_commands: list[str] | None = None,
        test_count: int = 0,
    ) -> StagedFeature:
        plan = self._load()
        for m in plan:
            if m.version == version:
                feature = StagedFeature(
                    name=name,
                    description=description,
                    tier=tier,
                    cli_commands=cli_commands or [],
                    test_count=test_count,
                )
                m.features.append(feature)
                self._save(plan)
                return feature
        raise ValueError(f"Milestone {version} not found")

    def mark_shipped(self, version: str) -> ReleaseMilestone:
        plan = self._load()
        for m in plan:
            if m.version == version:
                m.status = "tagged"
                for f in m.features:
                    f.status = "shipped"
                self._save(plan)
                return m
        raise ValueError(f"Milestone {version} not found")

    def mark_announced(self, version: str) -> ReleaseMilestone:
        plan = self._load()
        for m in plan:
            if m.version == version:
                m.status = "announced"
                for f in m.features:
                    f.status = "announced"
                self._save(plan)
                return m
        raise ValueError(f"Milestone {version} not found")

    def get_milestone(self, version: str) -> ReleaseMilestone | None:
        for m in self._load():
            if m.version == version:
                return m
        return None

    def list_milestones(self, status: str | None = None) -> list[ReleaseMilestone]:
        plan = self._load()
        if status:
            return [m for m in plan if m.status == status]
        return plan

    def generate_notes(self, version: str) -> str:
        """Auto-generate release notes for a milestone."""
        milestone = self.get_milestone(version)
        if milestone is None:
            raise ValueError(f"Milestone {version} not found")

        lines = [f"# Release {milestone.version}"]
        if milestone.codename:
            lines[0] += f" — {milestone.codename}"
        lines.append("")

        if milestone.features:
            lines.append("## What's New")
            lines.append("")
            for f in milestone.features:
                lines.append(f"### {f.name}")
                lines.append(f"{f.description}")
                if f.cli_commands:
                    lines.append("")
                    lines.append("**Commands:**")
                    for cmd in f.cli_commands:
                        lines.append(f"- `{cmd}`")
                lines.append("")

        total_tests = sum(f.test_count for f in milestone.features)
        if total_tests:
            lines.append("## Quality")
            lines.append(f"- {total_tests} tests covering new features")
            lines.append("")

        return "\n".join(lines)

    def next_milestone(self) -> ReleaseMilestone | None:
        """Get the next planned milestone."""
        for m in self._load():
            if m.status == "planned":
                return m
        return None

    def _load(self) -> list[ReleaseMilestone]:
        if not self._path.exists():
            return []
        data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        milestones = []
        for d in data:
            features = [
                StagedFeature(
                    name=f["name"],
                    description=f.get("description", ""),
                    tier=f.get("tier", 0),
                    status=f.get("status", "staged"),
                    cli_commands=f.get("cli_commands", []),
                    test_count=f.get("test_count", 0),
                )
                for f in d.get("features", [])
            ]
            milestones.append(
                ReleaseMilestone(
                    version=d["version"],
                    codename=d.get("codename", ""),
                    target_date=d.get("target_date", ""),
                    status=d.get("status", "planned"),
                    features=features,
                    notes=d.get("notes", ""),
                )
            )
        return milestones

    def _save(self, plan: list[ReleaseMilestone]) -> None:
        data = [m.to_dict() for m in plan]
        self._path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
