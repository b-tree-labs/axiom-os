# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for Course manifest loading + .axiompack format.

A Course manifest is a YAML file defining: objectives, corpus
references, assessments, onboarding rails, system prompt,
schedule, and extension requirements. It's the instructor's
blueprint for a classroom.

An .axiompack bundles the manifest + corpus files + questionnaires
into a distributable, versionable archive.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# 1. MANIFEST LOADING
# ---------------------------------------------------------------------------


class TestCourseManifestLoading:
    def test_load_minimal_manifest(self, tmp_path):
        from axiom.extensions.builtins.classroom.course_manifest import load_course_manifest

        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text(
            "id: ne-stem-2026\ntitle: STEM Course Prague 2026\nversion: 1.0.0\n"
        )
        course = load_course_manifest(manifest_path)
        assert course.id == "ne-stem-2026"
        assert course.title == "STEM Course Prague 2026"
        assert course.version == "1.0.0"

    def test_load_with_objectives(self, tmp_path):
        from axiom.extensions.builtins.classroom.course_manifest import load_course_manifest

        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text(
            "id: test\n"
            "title: Test\n"
            "version: 1.0.0\n"
            "objectives:\n"
            "  - id: obj-1\n"
            "    description: Understand core concepts\n"
            "  - id: obj-2\n"
            "    description: Apply research methodology\n"
        )
        course = load_course_manifest(manifest_path)
        assert len(course.objectives) == 2
        assert course.objectives[0]["id"] == "obj-1"

    def test_load_with_system_prompt(self, tmp_path):
        from axiom.extensions.builtins.classroom.course_manifest import load_course_manifest

        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text(
            "id: test\n"
            "title: Test\n"
            "version: 1.0.0\n"
            "system_prompt: You are a helpful tutor for this STEM course.\n"
        )
        course = load_course_manifest(manifest_path)
        assert "STEM course" in course.system_prompt

    def test_load_with_corpus_refs(self, tmp_path):
        from axiom.extensions.builtins.classroom.course_manifest import load_course_manifest

        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text(
            "id: test\n"
            "title: Test\n"
            "version: 1.0.0\n"
            "corpus:\n"
            "  - path: corpus/textbook.pdf\n"
            "    tier: public\n"
            "  - path: corpus/lab-data.csv\n"
            "    tier: restricted\n"
        )
        course = load_course_manifest(manifest_path)
        assert len(course.corpus) == 2
        assert course.corpus[0]["tier"] == "public"

    def test_load_with_onboarding_rails(self, tmp_path):
        from axiom.extensions.builtins.classroom.course_manifest import load_course_manifest

        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text(
            "id: test\n"
            "title: Test\n"
            "version: 1.0.0\n"
            "onboarding_rails:\n"
            "  - id: interview\n"
            "    source: custom\n"
            "    required: true\n"
            "    questions:\n"
            "      - id: Q1\n"
            "        text: Hello?\n"
            "        type: free_text\n"
        )
        course = load_course_manifest(manifest_path)
        assert len(course.onboarding_rails) == 1

    def test_load_with_assessments(self, tmp_path):
        from axiom.extensions.builtins.classroom.course_manifest import load_course_manifest

        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text(
            "id: test\n"
            "title: Test\n"
            "version: 1.0.0\n"
            "assessments:\n"
            "  - id: pre-quiz\n"
            "    type: quiz\n"
            "    title: Pre-Course Quiz\n"
            "    points: 100\n"
            "    scheduled: 2026-07-01\n"
            "  - id: mid-quiz\n"
            "    type: quiz\n"
            "    title: Mid-Course Quiz\n"
            "    points: 100\n"
            "    scheduled: 2026-07-15\n"
        )
        course = load_course_manifest(manifest_path)
        assert len(course.assessments) == 2
        assert course.assessments[0]["id"] == "pre-quiz"

    def test_missing_required_field_raises(self, tmp_path):
        from axiom.extensions.builtins.classroom.course_manifest import load_course_manifest

        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text("title: No ID\nversion: 1.0.0\n")

        with pytest.raises(ValueError, match="id"):
            load_course_manifest(manifest_path)


# ---------------------------------------------------------------------------
# 2. AXIOMPACK BUNDLING
# ---------------------------------------------------------------------------


class TestAxiomPackBundling:
    def test_create_pack_from_manifest(self, tmp_path):
        from axiom.extensions.builtins.classroom.course_manifest import (
            create_axiompack,
            load_course_manifest,
        )

        # Create manifest + a corpus file
        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text(
            "id: test-course\n"
            "title: Test\n"
            "version: 1.0.0\n"
            "corpus:\n"
            "  - path: corpus/notes.txt\n"
            "    tier: public\n"
        )
        (tmp_path / "corpus").mkdir()
        (tmp_path / "corpus" / "notes.txt").write_text("Lecture notes content.")

        course = load_course_manifest(manifest_path)
        pack_path = create_axiompack(course, source_dir=tmp_path, output_dir=tmp_path)

        assert pack_path.exists()
        assert pack_path.suffix == ".axiompack"
        assert "test-course" in pack_path.name

    def test_pack_contains_manifest_and_corpus(self, tmp_path):
        import zipfile

        from axiom.extensions.builtins.classroom.course_manifest import (
            create_axiompack,
            load_course_manifest,
        )

        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text(
            "id: pack-test\n"
            "title: Pack Test\n"
            "version: 2.0.0\n"
            "corpus:\n"
            "  - path: corpus/data.txt\n"
            "    tier: public\n"
        )
        (tmp_path / "corpus").mkdir()
        (tmp_path / "corpus" / "data.txt").write_text("Data content.")

        course = load_course_manifest(manifest_path)
        pack_path = create_axiompack(course, source_dir=tmp_path, output_dir=tmp_path)

        with zipfile.ZipFile(pack_path) as zf:
            names = zf.namelist()
            assert "MANIFEST.yaml" in names
            assert "corpus/data.txt" in names

    def test_pack_version_in_filename(self, tmp_path):
        from axiom.extensions.builtins.classroom.course_manifest import (
            create_axiompack,
            load_course_manifest,
        )

        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text("id: versioned\ntitle: V\nversion: 3.1.0\n")

        course = load_course_manifest(manifest_path)
        pack_path = create_axiompack(course, source_dir=tmp_path, output_dir=tmp_path)

        assert "3.1.0" in pack_path.name


# ---------------------------------------------------------------------------
# 3. AXIOMPACK LOADING (UNPACK)
# ---------------------------------------------------------------------------


class TestAxiomPackLoading:
    def test_load_pack_extracts_manifest(self, tmp_path):
        from axiom.extensions.builtins.classroom.course_manifest import (
            create_axiompack,
            load_axiompack,
            load_course_manifest,
        )

        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text("id: loadable\ntitle: Loadable\nversion: 1.0.0\n")

        course = load_course_manifest(manifest_path)
        pack_path = create_axiompack(course, source_dir=tmp_path, output_dir=tmp_path)

        # Load into a different directory
        extract_dir = tmp_path / "extracted"
        loaded = load_axiompack(pack_path, extract_dir=extract_dir)

        assert loaded.id == "loadable"
        assert loaded.version == "1.0.0"
        assert (extract_dir / "MANIFEST.yaml").exists()

    def test_load_pack_extracts_corpus(self, tmp_path):
        from axiom.extensions.builtins.classroom.course_manifest import (
            create_axiompack,
            load_axiompack,
            load_course_manifest,
        )

        manifest_path = tmp_path / "course.yaml"
        manifest_path.write_text(
            "id: with-corpus\ntitle: C\nversion: 1.0.0\n"
            "corpus:\n  - path: docs/notes.md\n    tier: public\n"
        )
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "notes.md").write_text("# Notes\nContent here.")

        course = load_course_manifest(manifest_path)
        pack_path = create_axiompack(course, source_dir=tmp_path, output_dir=tmp_path)

        extract_dir = tmp_path / "loaded"
        load_axiompack(pack_path, extract_dir=extract_dir)

        assert (extract_dir / "docs" / "notes.md").exists()
        assert "Content here" in (extract_dir / "docs" / "notes.md").read_text()
