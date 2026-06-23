# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom media ingest — multimedia → personal RAG timeline.

Students capture media during class (audio, video, photos, PDFs,
handwritten notes, chat exports) and ingest into their personal-node
RAG as a searchable, timestamped timeline.

Wraps existing SCAN infrastructure (VoiceExtractor, MediaLibrary)
and adds: OCR for images, PDF text extraction, text/chat-export
ingest, timeline view, and personal RAG chunk generation.

Processing is async-friendly: status transitions
pending → processing → processed (or unsupported/error).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SUPPORTED_MEDIA_TYPES = {
    "audio",
    "video",
    "image",
    "pdf",
    "text",
    "chat_export",
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class MediaItem:
    """A single media capture in a student's timeline."""

    student_id: str
    media_type: str  # one of SUPPORTED_MEDIA_TYPES
    source_path: str  # file path (empty for text/chat_export with raw_text)
    title: str
    raw_text: str = ""  # for text/chat_export: the content directly
    timestamp: str = ""  # ISO 8601; auto-set if empty
    status: str = "pending"  # pending, processing, processed, unsupported, error
    transcript: str | None = None  # for audio/video
    extracted_text: str | None = None  # for image (OCR) / pdf
    rag_chunks: list[dict[str, Any]] = field(default_factory=list)
    error_message: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Processing pipeline
# ---------------------------------------------------------------------------

# Injectable processor signatures
Transcriber = Callable[[Path], str]  # audio/video path → transcript text
OCRProcessor = Callable[[Path], str]  # image path → extracted text
PDFProcessor = Callable[[Path], str]  # pdf path → extracted text


@dataclass
class MediaIngestPipeline:
    """Processes media items into RAG-ready chunks.

    Processors are injectable for testing and for swapping backends
    (Whisper vs Deepgram, Tesseract vs cloud OCR, etc.).
    """

    transcriber: Transcriber | None = None
    ocr_processor: OCRProcessor | None = None
    pdf_processor: PDFProcessor | None = None
    chunk_size: int = 500  # characters per chunk

    def process(self, item: MediaItem) -> MediaItem:
        """Process a media item: extract text, generate RAG chunks."""
        if item.media_type not in SUPPORTED_MEDIA_TYPES:
            item.status = "unsupported"
            return item

        item.status = "processing"

        try:
            text = self._extract_text(item)
            if text:
                item.rag_chunks = self._chunk_text(text, item)
                if item.media_type in ("audio", "video"):
                    item.transcript = text
                elif item.media_type in ("image", "pdf"):
                    item.extracted_text = text
            item.status = "processed"
        except Exception as exc:
            item.status = "error"
            item.error_message = str(exc)

        return item

    def _extract_text(self, item: MediaItem) -> str:
        """Extract text from the media item based on its type."""
        if item.media_type in ("audio", "video"):
            if self.transcriber is None:
                return ""
            return self.transcriber(Path(item.source_path))

        if item.media_type == "image":
            if self.ocr_processor is None:
                return ""
            return self.ocr_processor(Path(item.source_path))

        if item.media_type == "pdf":
            if self.pdf_processor is None:
                return ""
            return self.pdf_processor(Path(item.source_path))

        if item.media_type in ("text", "chat_export"):
            return item.raw_text

        return ""

    def _chunk_text(self, text: str, item: MediaItem) -> list[dict[str, Any]]:
        """Split text into RAG-ready chunks with metadata."""
        if item.media_type == "chat_export":
            return self._chunk_chat(text, item)

        chunks = []
        words = text.split()
        current: list[str] = []
        current_len = 0

        for word in words:
            current.append(word)
            current_len += len(word) + 1
            if current_len >= self.chunk_size:
                chunks.append(
                    {
                        "text": " ".join(current),
                        "source": item.title,
                        "media_type": item.media_type,
                        "student_id": item.student_id,
                        "timestamp": item.timestamp,
                    }
                )
                current = []
                current_len = 0

        if current:
            chunks.append(
                {
                    "text": " ".join(current),
                    "source": item.title,
                    "media_type": item.media_type,
                    "student_id": item.student_id,
                    "timestamp": item.timestamp,
                }
            )

        return chunks

    def _chunk_chat(self, text: str, item: MediaItem) -> list[dict[str, Any]]:
        """Chunk a chat export by conversation turns."""
        chunks = []
        current_turn: list[str] = []

        for line in text.strip().split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            # New turn starts with "User:" or "Assistant:" (or similar speaker labels)
            if ":" in stripped and stripped.split(":")[0].strip() in (
                "User",
                "Assistant",
                "Student",
                "Instructor",
                "System",
            ):
                if current_turn:
                    chunks.append(
                        {
                            "text": "\n".join(current_turn),
                            "source": item.title,
                            "media_type": "chat_export",
                            "student_id": item.student_id,
                            "timestamp": item.timestamp,
                        }
                    )
                current_turn = [stripped]
            else:
                current_turn.append(stripped)

        if current_turn:
            chunks.append(
                {
                    "text": "\n".join(current_turn),
                    "source": item.title,
                    "media_type": "chat_export",
                    "student_id": item.student_id,
                    "timestamp": item.timestamp,
                }
            )

        return chunks


# ---------------------------------------------------------------------------
# Student timeline
# ---------------------------------------------------------------------------


@dataclass
class StudentTimeline:
    """Chronological timeline of all media a student has ingested."""

    student_id: str
    items: list[MediaItem] = field(default_factory=list)

    def add(self, item: MediaItem) -> None:
        """Add a media item to the timeline."""
        self.items.append(item)

    def get_chronological(self) -> list[MediaItem]:
        """Return items ordered by timestamp."""
        return sorted(self.items, key=lambda i: i.timestamp)

    def filter_by_type(self, media_type: str) -> list[MediaItem]:
        """Return items of a specific type."""
        return [i for i in self.items if i.media_type == media_type]

    def get_all_rag_chunks(self) -> list[dict[str, Any]]:
        """Aggregate RAG chunks from all processed items."""
        chunks = []
        for item in self.items:
            chunks.extend(item.rag_chunks)
        return chunks
