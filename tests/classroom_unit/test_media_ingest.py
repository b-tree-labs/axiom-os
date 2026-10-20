# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for classroom media ingest — multimedia → personal RAG timeline.

Students capture media during class (audio, video, photos, PDFs, notes,
chat exports) and ingest into their personal-node RAG as a searchable
timeline. Wraps existing SCAN VoiceExtractor + MediaLibrary.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. MEDIA ITEM MODEL
# ---------------------------------------------------------------------------


class TestMediaItemModel:
    def test_create_audio_item(self):
        from axiom.extensions.builtins.classroom.media_ingest import MediaItem

        item = MediaItem(
            student_id="s1",
            media_type="audio",
            source_path="/tmp/lecture.m4a",
            title="Tuesday lecture recording",
        )
        assert item.media_type == "audio"
        assert item.status == "pending"
        assert item.timestamp is not None
        assert item.rag_chunks == []

    def test_supported_media_types(self):
        from axiom.extensions.builtins.classroom.media_ingest import SUPPORTED_MEDIA_TYPES

        assert "audio" in SUPPORTED_MEDIA_TYPES
        assert "video" in SUPPORTED_MEDIA_TYPES
        assert "image" in SUPPORTED_MEDIA_TYPES
        assert "pdf" in SUPPORTED_MEDIA_TYPES
        assert "text" in SUPPORTED_MEDIA_TYPES
        assert "chat_export" in SUPPORTED_MEDIA_TYPES


# ---------------------------------------------------------------------------
# 2. PROCESSING PIPELINE
# ---------------------------------------------------------------------------


class TestProcessingPipeline:
    def test_process_audio_produces_transcript(self, tmp_path):
        from axiom.extensions.builtins.classroom.media_ingest import (
            MediaIngestPipeline,
            MediaItem,
        )

        audio_file = tmp_path / "lecture.wav"
        audio_file.write_bytes(b"fake audio data")

        pipeline = MediaIngestPipeline(
            transcriber=lambda path: "The instructor discussed fission reactions today.",
            ocr_processor=None,
            pdf_processor=None,
        )

        item = MediaItem(
            student_id="s1",
            media_type="audio",
            source_path=str(audio_file),
            title="Lecture recording",
        )

        processed = pipeline.process(item)

        assert processed.status == "processed"
        assert processed.transcript is not None
        assert "fission" in processed.transcript
        assert len(processed.rag_chunks) > 0

    def test_process_image_produces_ocr_text(self, tmp_path):
        from axiom.extensions.builtins.classroom.media_ingest import (
            MediaIngestPipeline,
            MediaItem,
        )

        img_file = tmp_path / "whiteboard.jpg"
        img_file.write_bytes(b"fake image data")

        pipeline = MediaIngestPipeline(
            transcriber=None,
            ocr_processor=lambda path: "E = mc² \nFission cross section σ_f",
            pdf_processor=None,
        )

        item = MediaItem(
            student_id="s1",
            media_type="image",
            source_path=str(img_file),
            title="Whiteboard photo",
        )

        processed = pipeline.process(item)
        assert processed.status == "processed"
        assert processed.extracted_text is not None
        assert "E = mc²" in processed.extracted_text
        assert len(processed.rag_chunks) > 0

    def test_process_pdf_extracts_text(self, tmp_path):
        from axiom.extensions.builtins.classroom.media_ingest import (
            MediaIngestPipeline,
            MediaItem,
        )

        pdf_file = tmp_path / "handout.pdf"
        pdf_file.write_bytes(b"fake pdf data")

        pipeline = MediaIngestPipeline(
            transcriber=None,
            ocr_processor=None,
            pdf_processor=lambda path: (
                "Chapter 1: Introduction to Nuclear Physics\nAtoms consist of..."
            ),
        )

        item = MediaItem(
            student_id="s1",
            media_type="pdf",
            source_path=str(pdf_file),
            title="Course handout",
        )

        processed = pipeline.process(item)
        assert processed.status == "processed"
        assert "Nuclear Physics" in processed.extracted_text
        assert len(processed.rag_chunks) > 0

    def test_process_text_note(self):
        from axiom.extensions.builtins.classroom.media_ingest import (
            MediaIngestPipeline,
            MediaItem,
        )

        pipeline = MediaIngestPipeline()

        item = MediaItem(
            student_id="s1",
            media_type="text",
            source_path="",
            title="My notes from today",
            raw_text="Key takeaway: chain reactions need moderation. Q: what is the role of water?",
        )

        processed = pipeline.process(item)
        assert processed.status == "processed"
        assert len(processed.rag_chunks) > 0
        assert "chain reactions" in processed.rag_chunks[0]["text"]

    def test_process_chat_export(self):
        from axiom.extensions.builtins.classroom.media_ingest import (
            MediaIngestPipeline,
            MediaItem,
        )

        pipeline = MediaIngestPipeline()

        chat_text = (
            "User: What is criticality?\n"
            "Assistant: Criticality is when a nuclear chain reaction is self-sustaining.\n"
            "User: How do you control it?\n"
            "Assistant: Control rods absorb neutrons to regulate the reaction rate.\n"
        )

        item = MediaItem(
            student_id="s1",
            media_type="chat_export",
            source_path="",
            title="Chat session on criticality",
            raw_text=chat_text,
        )

        processed = pipeline.process(item)
        assert processed.status == "processed"
        assert len(processed.rag_chunks) >= 2  # at least 2 turns chunked

    def test_unsupported_type_graceful(self):
        from axiom.extensions.builtins.classroom.media_ingest import (
            MediaIngestPipeline,
            MediaItem,
        )

        pipeline = MediaIngestPipeline()
        item = MediaItem(
            student_id="s1",
            media_type="unknown_format",
            source_path="",
            title="Weird file",
        )

        processed = pipeline.process(item)
        assert processed.status == "unsupported"


# ---------------------------------------------------------------------------
# 3. TIMELINE
# ---------------------------------------------------------------------------


class TestTimeline:
    def test_add_items_to_timeline(self):
        from axiom.extensions.builtins.classroom.media_ingest import (
            MediaItem,
            StudentTimeline,
        )

        timeline = StudentTimeline(student_id="s1")

        timeline.add(
            MediaItem(student_id="s1", media_type="audio", source_path="a.wav", title="Lecture 1")
        )
        timeline.add(
            MediaItem(student_id="s1", media_type="image", source_path="b.jpg", title="Board photo")
        )
        timeline.add(
            MediaItem(
                student_id="s1",
                media_type="text",
                source_path="",
                title="Notes",
                raw_text="Some notes",
            )
        )

        assert len(timeline.items) == 3

    def test_timeline_ordered_by_timestamp(self):
        from axiom.extensions.builtins.classroom.media_ingest import (
            MediaItem,
            StudentTimeline,
        )

        timeline = StudentTimeline(student_id="s1")
        item1 = MediaItem(student_id="s1", media_type="text", source_path="", title="First")
        item2 = MediaItem(student_id="s1", media_type="text", source_path="", title="Second")

        timeline.add(item1)
        timeline.add(item2)

        ordered = timeline.get_chronological()
        assert ordered[0].title == "First"
        assert ordered[1].title == "Second"

    def test_timeline_filter_by_type(self):
        from axiom.extensions.builtins.classroom.media_ingest import (
            MediaItem,
            StudentTimeline,
        )

        timeline = StudentTimeline(student_id="s1")
        timeline.add(
            MediaItem(student_id="s1", media_type="audio", source_path="a.wav", title="Audio")
        )
        timeline.add(MediaItem(student_id="s1", media_type="text", source_path="", title="Notes"))
        timeline.add(
            MediaItem(student_id="s1", media_type="audio", source_path="b.wav", title="Audio 2")
        )

        audio_only = timeline.filter_by_type("audio")
        assert len(audio_only) == 2

    def test_timeline_rag_chunks_aggregated(self):
        from axiom.extensions.builtins.classroom.media_ingest import (
            MediaItem,
            StudentTimeline,
        )

        timeline = StudentTimeline(student_id="s1")
        item = MediaItem(
            student_id="s1",
            media_type="text",
            source_path="",
            title="Notes",
            rag_chunks=[{"text": "Chunk 1", "source": "notes"}],
            status="processed",
        )
        timeline.add(item)

        all_chunks = timeline.get_all_rag_chunks()
        assert len(all_chunks) == 1
        assert all_chunks[0]["source"] == "notes"
