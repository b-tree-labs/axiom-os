# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom publish + enhanced dry-run — FW-1 P5.

Publishing transitions a prepared classroom into the ``published``
state, gated on both course-readiness AND classroom-readiness. Once
published, the classroom is considered bound to its course version
and ready for student enrollment.

Enhanced dry-run is a polished version of the existing dry-run CLI
that uses the course's actual corpus for retrieval (not a stub) so
the instructor sees realistic grounded answers before publishing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------


UNPUBLISHED = "unpublished"
PUBLISHED = "published"


# ---------------------------------------------------------------------------
# State getters/setters
# ---------------------------------------------------------------------------


def get_classroom_state(classroom_id: str) -> str:
    """Return the classroom's publish state (UNPUBLISHED or PUBLISHED)."""
    from .operational_store import load_classroom_data

    data = load_classroom_data(classroom_id)
    if data is None:
        return UNPUBLISHED
    return data.get("state") or UNPUBLISHED


def _persist_state(classroom_id: str, data: dict, **updates: Any) -> None:
    from .operational_store import _reg

    merged = dict(data)
    merged.update(updates)
    _reg().register(kind="classroom", name=classroom_id, data=merged)


# ---------------------------------------------------------------------------
# Publish gating
# ---------------------------------------------------------------------------


def _is_ready(classroom_id: str) -> tuple[bool, list[str]]:
    """Return (ready, blockers). ready is True iff course + classroom both publishable."""
    from .operational_store import load_classroom, load_course

    classroom_loaded = load_classroom(classroom_id)
    if classroom_loaded is None:
        return False, [f"classroom {classroom_id!r} not found"]
    classroom_wf, classroom_data = classroom_loaded

    blockers: list[str] = []

    classroom_ready, classroom_blockers = classroom_wf.is_ready_for_enrollment()
    blockers.extend(f"classroom:{b}" for b in classroom_blockers)

    course_id = classroom_data.get("course_id", "")
    course_ready = False
    if course_id:
        course_loaded = load_course(course_id)
        if course_loaded is not None:
            course_wf, _ = course_loaded
            course_ready, course_blockers = course_wf.is_ready_to_publish()
            blockers.extend(f"course:{b}" for b in course_blockers)
        else:
            blockers.append(f"course:{course_id}:not-found")
    else:
        blockers.append("classroom:no course_id")

    return (classroom_ready and course_ready), blockers


# ---------------------------------------------------------------------------
# Publish / unpublish
# ---------------------------------------------------------------------------


def publish_classroom(
    *, classroom_id: str, approver: str,
) -> dict[str, Any]:
    """Transition the classroom from prep → published.

    Fails if either the course OR the classroom checklist is
    incomplete. Always returns a dict with ``published: bool``; an
    error key carries the reason on failure.
    """
    from .operational_store import load_classroom_data

    if not classroom_id or not approver:
        return {
            "published": False,
            "error": "classroom_id and approver are both required",
        }

    data = load_classroom_data(classroom_id)
    if data is None:
        return {
            "published": False,
            "error": f"classroom {classroom_id!r} not found",
        }

    # ARCHIVED is terminal — republishing would re-open a cohort whose
    # record the harvest/analytics phases rely on staying frozen.
    from .archive import ARCHIVED

    if data.get("state") == ARCHIVED:
        return {
            "published": False,
            "error": (
                f"classroom {classroom_id!r} is archived; clone into a new "
                f"classroom via `classroom prep from-demo` or similar to "
                f"start a fresh cohort"
            ),
        }

    ready, blockers = _is_ready(classroom_id)
    if not ready:
        return {
            "published": False,
            "error": f"classroom not ready to publish: {'; '.join(blockers)}",
            "blockers": blockers,
        }

    now = datetime.now(UTC).isoformat()
    _persist_state(
        classroom_id, data,
        state=PUBLISHED,
        published_by=approver,
        published_at=now,
    )
    return {
        "published": True,
        "classroom_id": classroom_id,
        "approver": approver,
        "published_at": now,
        "state": PUBLISHED,
    }


def unpublish_classroom(*, classroom_id: str) -> dict[str, Any]:
    """Revert a published classroom to unpublished (prep mode)."""
    from .operational_store import load_classroom_data

    data = load_classroom_data(classroom_id)
    if data is None:
        return {"unpublished": False, "error": f"classroom {classroom_id!r} not found"}

    _persist_state(
        classroom_id, data,
        state=UNPUBLISHED,
        published_by=None,
        published_at=None,
    )
    return {
        "unpublished": True,
        "classroom_id": classroom_id,
        "state": UNPUBLISHED,
    }


# ---------------------------------------------------------------------------
# Enhanced dry-run
# ---------------------------------------------------------------------------


_DEFAULT_QUERIES: list[str] = [
    "What is Newton's second law?",
    "How does conservation of momentum apply to collisions?",
    "What is the work-energy theorem?",
]


def _derive_queries_from_corpus(
    corpus: list[dict[str, Any]],
    max_n: int = 3,
) -> list[str]:
    """Build sample dry-run queries from the corpus titles + topic terms.

    The hard-coded ``_DEFAULT_QUERIES`` are physics questions — fine
    for the demo, misleading for any real classroom. Deriving queries
    from the actual material keeps the dry-run honest: if the
    instructor uploaded reactor docs, the dry-run asks reactor
    questions. Falls back to generic affordances when the corpus is
    too sparse to derive from.
    """
    if not corpus:
        return list(_DEFAULT_QUERIES)

    queries: list[str] = []
    seen: set[str] = set()
    for doc in corpus[:max_n]:
        title = (doc.get("title") or "").strip()
        if not title:
            continue
        # Strip extension if the title is a filename like reactor.txt.
        clean = title.rsplit(".", 1)[0] if "." in title else title
        clean = clean.replace("_", " ").replace("-", " ").strip()
        if not clean or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        queries.append(f"What is {clean}?")
        if len(queries) >= max_n:
            break

    if not queries:
        # Last-resort generics — work for any corpus.
        return [
            "What's the main idea covered in this material?",
            "What are the key concepts I should focus on?",
            "Give me an overview of this topic.",
        ][:max_n]
    return queries


class _CorpusRetriever:
    """Retrieves from a list of document dicts (title + text) by keyword match."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def retrieve(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        q = query.lower()
        scored: list[tuple[int, dict[str, Any]]] = []
        tokens = [t for t in q.split() if len(t) > 2]
        for d in self._docs:
            text = (d.get("text") or "").lower()
            score = sum(1 for t in tokens if t in text)
            if score > 0:
                scored.append((score, d))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        hits = [d for _, d in scored[:k]]
        if hits:
            return hits
        # Fall through: no keyword hit — return the first k docs so the
        # instructor sees *something*, matching the existing CLI's behavior.
        return list(self._docs[:k])


def _demo_llm(messages: list[dict], **_: Any) -> str:
    """Deterministic stub LLM that cites the first retrieved doc.

    Good enough for the dry-run visualization — the goal is to show
    the instructor the wiring works end-to-end, not to evaluate the
    model. Real chat goes through the gateway once the classroom is
    published.
    """
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    user_msg = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    ctx_line = ""
    for s in sys_msgs:
        text = s.get("content", "")
        if text.startswith("Retrieved context:"):
            first = text.split("\n", 2)[1] if "\n" in text else ""
            if first:
                ctx_line = first[:200]
                break
    if ctx_line:
        return (
            f"[dry-run] Answering '{user_msg[:80]}' grounded on: "
            f"{ctx_line}..."
        )
    return f"[dry-run] Answering '{user_msg[:80]}' (no retrieved context matched)."


def enhanced_dry_run(
    *,
    classroom_id: str,
    queries: list[str] | None = None,
) -> dict[str, Any]:
    """Run the classroom's dry-run against the instructor's actual corpus.

    Resolution order for the corpus:
    1. Coordinator-side materials store (`~/.axi/coordinator/classrooms/
       <id>/`) — the files uploaded via `axi classroom prep corpus
       --upload`. This is where real instructor content lives.
    2. ``course_data["manifest"]["corpus"]`` — declarative corpus
       embedded in the course manifest (rarely populated in v0).
    3. ``DEMO_CORPUS`` — last-resort fallback so the dry-run has
       *something* to retrieve against. Only fires when the instructor
       hasn't uploaded any materials yet AND isn't running on the
       demo classroom.

    Without (1) the dry-run was misleading: an instructor uploading
    reactor-physics docs got Newton's-laws answers because the demo
    corpus was used.
    """
    from .demo import DEMO_CORPUS, DEMO_COURSE_ID
    from .operational_store import load_classroom, load_course_data

    classroom_loaded = load_classroom(classroom_id)
    if classroom_loaded is None:
        return {"error": f"classroom {classroom_id!r} not found"}
    classroom_wf, _classroom_data = classroom_loaded

    course_id = classroom_wf.course_id or ""
    course_data = load_course_data(course_id) if course_id else None

    # 1. Read uploaded materials from the coordinator-side store.
    corpus = _load_materials_corpus(classroom_id)

    # 2. Fall back to manifest-declared corpus if the materials store
    #    is empty.
    if not corpus and course_data is not None:
        manifest_corpus = (course_data.get("manifest") or {}).get("corpus") or []
        corpus = list(manifest_corpus)

    # 3. Last resort: demo corpus, so the dry-run still shows wiring
    #    end-to-end. Demo classroom always pins to the canonical
    #    demo corpus even if step 1 happens to find no files.
    if not corpus or course_id == DEMO_COURSE_ID:
        corpus = list(DEMO_CORPUS)

    classroom_wf.retriever = _CorpusRetriever(corpus)
    classroom_wf.llm = _demo_llm

    if queries:
        sample = list(queries)
    elif course_id == DEMO_COURSE_ID:
        # Demo classroom keeps its canonical physics queries — they
        # match the demo corpus exactly and are part of the seeded
        # skeptic-evaluation experience.
        sample = list(_DEFAULT_QUERIES)
    else:
        sample = _derive_queries_from_corpus(corpus)

    try:
        result = classroom_wf.run_dry_run(sample_queries=sample)
    except RuntimeError as e:
        return {"error": str(e)}

    return {
        "classroom_id": classroom_id,
        "turns": result.turns,
        "transcript": result.transcript,
    }


def _load_materials_corpus(classroom_id: str) -> list[dict[str, Any]]:
    """Read uploaded materials from the coordinator-side store.

    Returns a list of ``{"id", "title", "text"}`` dicts shaped for the
    dry-run retriever. Returns an empty list if the store doesn't
    exist or has no entries — caller handles fallback.
    """
    from pathlib import Path

    from .classroom_materials import ClassroomMaterialsStore

    coord_dir = (
        Path.home() / ".axi" / "coordinator"
        / "classrooms" / classroom_id
    )
    if not coord_dir.is_dir():
        return []
    materials = ClassroomMaterialsStore(coord_dir)
    docs: list[dict[str, Any]] = []
    for entry in materials.list_entries():
        try:
            content = materials.get_path(entry.file_id).read_bytes()
            text = content.decode("utf-8", errors="replace")
        except (OSError, KeyError):
            continue
        docs.append({
            "id": entry.file_id,
            "title": entry.title or entry.filename,
            "text": text,
        })
    return docs
