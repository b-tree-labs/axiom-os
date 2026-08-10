# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Course manifest + .axiompack format.

A Course manifest (YAML) defines the instructor's blueprint:
objectives, corpus, assessments, onboarding rails, system prompt.

An .axiompack bundles the manifest + corpus files into a distributable,
versionable zip archive for federation sync + student distribution.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CourseManifest:
    """Parsed course manifest."""

    id: str
    title: str
    version: str
    system_prompt: str = ""
    objectives: list[dict[str, Any]] = field(default_factory=list)
    corpus: list[dict[str, Any]] = field(default_factory=list)
    onboarding_rails: list[dict[str, Any]] = field(default_factory=list)
    assessments: list[dict[str, Any]] = field(default_factory=list)
    schedule: dict[str, Any] = field(default_factory=dict)
    extensions: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_course_manifest(path: Path) -> CourseManifest:
    """Load and validate a Course manifest from a YAML file."""
    raw = yaml.safe_load(path.read_text()) or {}

    # Required fields
    if "id" not in raw:
        raise ValueError("Course manifest missing required field: 'id'")
    if "title" not in raw:
        raise ValueError("Course manifest missing required field: 'title'")
    if "version" not in raw:
        raise ValueError("Course manifest missing required field: 'version'")

    return CourseManifest(
        id=raw["id"],
        title=raw["title"],
        version=raw["version"],
        system_prompt=raw.get("system_prompt", ""),
        objectives=raw.get("objectives", []),
        corpus=raw.get("corpus", []),
        onboarding_rails=raw.get("onboarding_rails", []),
        assessments=raw.get("assessments", []),
        schedule=raw.get("schedule", {}),
        extensions=raw.get("extensions", []),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# .axiompack creation (zip archive)
# ---------------------------------------------------------------------------


def create_axiompack(
    course: CourseManifest,
    source_dir: Path,
    output_dir: Path,
) -> Path:
    """Bundle a Course manifest + referenced files into an .axiompack zip.

    The pack contains:
    - MANIFEST.yaml (the course manifest)
    - All corpus files referenced by relative path
    - Any questionnaire YAML files referenced by onboarding rails

    Returns the path to the created .axiompack file.
    """
    pack_name = f"{course.id}-v{course.version}.axiompack"
    pack_path = output_dir / pack_name

    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Write the manifest itself
        manifest_yaml = yaml.dump(course.raw, default_flow_style=False)
        zf.writestr("MANIFEST.yaml", manifest_yaml)

        # Bundle corpus files
        for corpus_ref in course.corpus:
            rel_path = corpus_ref.get("path", "")
            abs_path = source_dir / rel_path
            if abs_path.exists():
                zf.write(abs_path, rel_path)

    return pack_path


# ---------------------------------------------------------------------------
# .axiompack loading (unzip + parse manifest)
# ---------------------------------------------------------------------------


def load_axiompack(pack_path: Path, extract_dir: Path) -> CourseManifest:
    """Extract an .axiompack and return the parsed CourseManifest.

    All files are extracted to extract_dir. The MANIFEST.yaml is
    parsed and returned as a CourseManifest.
    """
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(pack_path, "r") as zf:
        zf.extractall(extract_dir)

    manifest_path = extract_dir / "MANIFEST.yaml"
    if not manifest_path.exists():
        raise ValueError(f"No MANIFEST.yaml found in {pack_path}")

    return load_course_manifest(manifest_path)
