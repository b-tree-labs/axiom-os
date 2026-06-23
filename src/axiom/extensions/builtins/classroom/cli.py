# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axi classroom — CLI dispatcher for the classroom extension.

Primary verbs (instructor-facing operational flow):
  axi classroom prep init [--title] [--instructor] [--from]
      Start a new classroom. Generates uuid + slug for course and
      classroom. Prints "→ creating course / classroom" for
      incremental revelation — users don't need to learn the
      course-vs-classroom distinction up front.
  axi classroom prep status <classroom-id>
      Show unified checklist (course prep + classroom prep) for
      a single classroom.
  axi classroom prep corpus|prompt|assessment|rails
      Course template steps — affect the underlying course artifact.
  axi classroom prep rag|lms|dry-run
      Classroom instance steps — affect only this instance.

Course-centric power operations (share, publish, version) are under
`axi course` (future).

State lives in two places:
  runtime/courses/<course-id>/course.json
  runtime/classrooms/<classroom-id>/classroom.json
The classroom file holds a pointer to course_id@version.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from axiom.infra.identifiers import create_identity

from .classroom_prep_workflow import ClassroomPrepWorkflow
from .course_prep_workflow import CoursePrepWorkflow

# ---------------------------------------------------------------------------
# Runtime paths
# ---------------------------------------------------------------------------


def _runtime_root() -> Path:
    override = os.environ.get("AXIOM_RUNTIME_ROOT")
    if override:
        return Path(override)
    try:
        from axiom import REPO_ROOT  # type: ignore

        return Path(REPO_ROOT) / "runtime"
    except Exception:
        return Path.cwd() / "runtime"


def _course_path(course_id: str) -> Path:
    return _runtime_root() / "courses" / course_id / "course.json"


def _classroom_path(classroom_id: str) -> Path:
    return _runtime_root() / "classrooms" / classroom_id / "classroom.json"


# ---------------------------------------------------------------------------
# Stub backends (offline manual-test mode)
# ---------------------------------------------------------------------------


class _StubIndexer:
    def __init__(self) -> None:
        self._n = 0

    def index(self, documents: list[dict]) -> int:
        self._n += len(documents)
        return self._n


class _StubRetriever:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def retrieve(self, query: str, k: int = 5) -> list[dict]:
        lower = query.lower()
        hits = [d for d in self._docs if lower in d.get("text", "").lower()]
        return hits[:k] if hits else self._docs[:k]


def _stub_llm(messages: list[dict], **kw: Any) -> str:
    user_msg = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    return f"[stub LLM response to: {user_msg[:80]}]"


class _StubLMS:
    def __init__(self, roster_size: int) -> None:
        self._roster = [
            {"id": f"s{i+1}", "name": f"Student {i+1}", "email": f"s{i+1}@ut.edu"}
            for i in range(roster_size)
        ]

    def ping(self) -> bool:
        return True

    def list_students(self, course_id: str) -> list[dict]:
        return list(self._roster)


class _StubLMSOffline(_StubLMS):
    def ping(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Course + classroom state persistence
# ---------------------------------------------------------------------------
# Operational state (course + classroom checklists, manifests, roster state)
# now lives in operational_store (ArtifactRegistry-backed, SQLite at
# runtime/operational.db). The earlier JSON-file layout is gone.
# Per ADR-029 composition: operational state is an Artifact; memory
# fragments reference it.


def _save_course(wf: CoursePrepWorkflow, slug: str, title: str | None) -> None:
    from .operational_store import save_course as _save

    _save(wf, slug=slug, title=title)


def _load_course(course_id: str) -> tuple[CoursePrepWorkflow, dict] | None:
    from .operational_store import load_course as _load

    return _load(course_id)


def _save_classroom(
    wf: ClassroomPrepWorkflow,
    slug: str,
    title: str | None,
    course_id: str,
    course_slug: str,
) -> None:
    from .operational_store import save_classroom as _save

    _save(
        wf, slug=slug, title=title, course_id=course_id, course_slug=course_slug,
    )


def _maybe_select_course_now(
    classroom_wf: ClassroomPrepWorkflow,
    classroom_data: dict,
    course_wf: CoursePrepWorkflow,
    course_data: dict,
) -> bool:
    """Auto-mark ``course_selected`` once the course is publishable.

    Defends against the prep-step-ordering bug where the first
    ``prep prompt --approve`` happened when the course wasn't yet
    publishable (e.g., corpus not indexed yet). Without this, the
    classroom's ``course_selected`` checklist item stays pending
    forever and the user gets stuck — there's no obvious command to
    move it green. Calling this from every prep handler that touches
    course state keeps the classroom checklist self-healing.

    Returns True if state was changed (saved).
    """
    course_ready, _ = course_wf.is_ready_to_publish()
    if not course_ready:
        return False
    classroom_step_state = (
        classroom_data.get("checklist", {})
        .get("course_selected", {})
        .get("status", "pending")
    )
    if classroom_step_state == "completed":
        return False

    classroom_wf.select_course(
        course_id=course_wf.course_id,
        course_version=(course_wf.manifest or {}).get("version", "1.0.0"),
        publishable=True,
        system_prompt=course_wf.system_prompt,
    )
    _save_classroom(
        classroom_wf,
        slug=classroom_data["slug"],
        title=classroom_data.get("title"),
        course_id=classroom_data["course_id"],
        course_slug=course_data["slug"],
    )
    return True


def _load_classroom(classroom_id: str) -> tuple[ClassroomPrepWorkflow, dict] | None:
    from .operational_store import load_classroom as _load

    return _load(classroom_id)


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


def _load_manifest_file(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"manifest file not found: {path}")
    text = p.read_text()
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore

            return yaml.safe_load(text) or {}
        except ImportError:
            data = {}
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    data[k.strip()] = v.strip().strip('"').strip("'")
            return data
    return json.loads(text)


# ---------------------------------------------------------------------------
# Resolve --from (existing classroom or course) → course data
# ---------------------------------------------------------------------------


def _resolve_from(from_ref: str) -> dict | None:
    """Try to resolve --from as a classroom first, then a course."""
    # Try classroom lookup
    loaded = _load_classroom(from_ref)
    if loaded is not None:
        _, classroom_data = loaded
        course_id = classroom_data.get("course_id")
        if course_id:
            course_loaded = _load_course(course_id)
            if course_loaded:
                _, course_data = course_loaded
                return course_data

    # Try course lookup directly
    course_loaded = _load_course(from_ref)
    if course_loaded:
        _, course_data = course_loaded
        return course_data

    return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    from . import ui

    # Resolve reuse-existing-course path
    existing_course: dict | None = None
    if args.from_ref:
        existing_course = _resolve_from(args.from_ref)
        if existing_course is None:
            ui.emit_error(
                f"--from target '{args.from_ref}' not found as classroom or course"
            )
            return 1

    # Create course identity (unless reusing)
    if existing_course:
        course_id = existing_course["id"]
        course_slug = existing_course["slug"]
        course_title = existing_course.get("title")
        ui.emit_info(f"Reusing course \"{course_slug}\" ({course_id[:8]}…)")
    else:
        course_identity = create_identity(title=args.title)
        course_id = course_identity["id"]
        course_slug = course_identity["slug"]
        course_title = args.title
        ui.emit_info(f"Creating course \"{course_slug}\" v1.0.0…")

    # Create classroom identity (always new)
    classroom_title = args.title  # classroom shares initial title
    classroom_identity = create_identity(title=classroom_title)
    classroom_id = classroom_identity["id"]
    classroom_slug = classroom_identity["slug"]
    ui.emit_info(f"Creating classroom \"{classroom_slug}\"…")

    # Build + save course workflow
    if existing_course:
        # Rehydrate existing course — no new state needed, just confirm path
        course_loaded = _load_course(course_id)
        assert course_loaded is not None
        course_wf, _ = course_loaded
    else:
        course_wf = CoursePrepWorkflow(
            instructor_id=args.instructor,
            course_id=course_id,
            indexer=_StubIndexer(),
            retriever=_StubRetriever([]),
            llm=_stub_llm,
        )
        # Auto-load manifest if provided
        if args.manifest:
            try:
                manifest = _load_manifest_file(args.manifest)
            except FileNotFoundError as e:
                print(str(e), file=sys.stderr)
                return 1
            course_wf.load_manifest(manifest)
        else:
            # Stamp in a minimal manifest so the course step advances
            course_wf.load_manifest({
                "id": course_id,
                "slug": course_slug,
                "title": course_title or course_slug,
                "version": "1.0.0",
            })
        _save_course(course_wf, slug=course_slug, title=course_title)

    # Build + save classroom workflow
    classroom_wf = ClassroomPrepWorkflow(
        instructor_id=args.instructor,
        classroom_id=classroom_id,
        retriever=_StubRetriever([]),
        llm=_stub_llm,
        lms=_StubLMSOffline(0),
    )
    # If reusing an existing publishable course, select it immediately
    if existing_course:
        course_ready, _ = course_wf.is_ready_to_publish()
        classroom_wf.select_course(
            course_id=course_id,
            course_version=existing_course.get("manifest", {}).get("version", "1.0.0"),
            publishable=course_ready,
            system_prompt=existing_course.get("system_prompt"),
        )

    # Optional: LMS-assisted prefill (simulated — real Canvas wires in later).
    if args.lms_assist_fake and not existing_course:
        ui.out().print()
        ui.emit_info("Simulated LMS (Canvas) import…")
        fake_docs = [
            {"text": "Course syllabus: nuclear engineering fundamentals.",
             "source": "syllabus.pdf"},
            {"text": "Chapter 1: Fission splits heavy nuclei.",
             "source": "chapter1.pdf"},
            {"text": "Chapter 2: Fusion combines light nuclei.",
             "source": "chapter2.pdf"},
        ]
        course_wf.indexer = _StubIndexer()
        course_wf.retriever = _StubRetriever(fake_docs)
        course_wf.upload_corpus(fake_docs)
        course_wf.preview_corpus("fission")
        ui.emit_success(f"Imported {len(fake_docs)} files as course corpus.")

        fake_assessments = [
            {"id": "pre-quiz", "type": "quiz", "week": 0},
            {"id": "mid-quiz", "type": "quiz", "week": 2},
            {"id": "post-quiz", "type": "quiz", "week": 4},
        ]
        for a in fake_assessments:
            course_wf.define_assessment(a)
        ui.emit_success(f"Imported {len(fake_assessments)} assignments as assessments.")

        _save_course(course_wf, slug=course_slug, title=course_title)

        roster_size = 15
        classroom_wf.lms = _StubLMS(roster_size=roster_size)
        classroom_wf.connect_lms(course_id="CANVAS-SIM-101")
        ui.emit_success(f"Rostered {roster_size} students via LMS connection.")

    _save_classroom(
        classroom_wf, slug=classroom_slug, title=classroom_title,
        course_id=course_id, course_slug=course_slug,
    )

    ui.emit_kv("Created", {
        "Course":    f"{course_slug}  (id: {course_id})",
        "Classroom": f"{classroom_slug}  (id: {classroom_id})",
    })
    next_steps = []
    if not existing_course:
        next_steps.append(
            f"axi classroom prep corpus {classroom_id} --upload <file> --preview <query>"
        )
        next_steps.append(
            f"axi classroom prep prompt {classroom_id} --set '...' --test '...'"
        )
    next_steps.extend([
        f"axi classroom prep rag    {classroom_id} --mode course_only",
        f"axi classroom prep lms    {classroom_id} --canvas-course <id> --fake",
        f"axi classroom prep status {classroom_id}",
    ])
    ui.emit_next_steps(next_steps)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    # T0-23: --tmux emits a single-line status for tmux status-right
    # embedding. No classroom_id means "list mode".
    if getattr(args, "tmux", False):
        from axiom.extensions.builtins.classroom.status_line import (
            tmux_status_line,
        )

        print(tmux_status_line(classroom_id=args.classroom_id or None))
        return 0

    if not args.classroom_id:
        print("usage: axi classroom prep status <classroom_id>", file=sys.stderr)
        return 1

    loaded = _load_classroom(args.classroom_id)
    if loaded is None:
        print(
            f"No classroom prep session found for '{args.classroom_id}'. "
            f"Run `axi classroom prep init` first."
        )
        return 1
    classroom_wf, classroom_data = loaded
    course_loaded = _load_course(classroom_data["course_id"])
    if course_loaded is None:
        print(f"Classroom references course {classroom_data['course_id']} "
              f"but course state is missing")
        return 1
    course_wf, course_data = course_loaded

    # Self-heal `course_selected` if a previous prep step landed it in a
    # stuck state (course publishable but classroom still pending).
    if _maybe_select_course_now(
        classroom_wf, classroom_data, course_wf, course_data,
    ):
        # Reload so the printed summary reflects the just-saved state.
        loaded2 = _load_classroom(args.classroom_id)
        if loaded2 is not None:
            classroom_wf, classroom_data = loaded2

    _print_unified_summary(course_wf, course_data, classroom_wf, classroom_data)

    course_ready, _ = course_wf.is_ready_to_publish()
    class_ready, _ = classroom_wf.is_ready_for_enrollment()
    return 0 if (course_ready and class_ready) else 2


# --- Course-template-step commands (act on underlying course) ---------------


def _load_classroom_and_course(
    classroom_id: str,
) -> tuple[ClassroomPrepWorkflow, dict, CoursePrepWorkflow, dict] | None:
    loaded = _load_classroom(classroom_id)
    if loaded is None:
        print(f"No classroom '{classroom_id}'", file=sys.stderr)
        return None
    classroom_wf, classroom_data = loaded
    course_loaded = _load_course(classroom_data["course_id"])
    if course_loaded is None:
        print(
            f"Course {classroom_data['course_id']} missing for classroom {classroom_id}",
            file=sys.stderr,
        )
        return None
    course_wf, course_data = course_loaded
    return classroom_wf, classroom_data, course_wf, course_data


def _cmd_corpus(args: argparse.Namespace) -> int:
    from . import ui
    from .classroom_materials import ClassroomMaterialsStore

    if not _require_active(args.classroom_id):
        return 1
    loaded = _load_classroom_and_course(args.classroom_id)
    if loaded is None:
        return 1
    classroom_wf, classroom_data, course_wf, course_data = loaded

    # Coordinator-side materials root: one subdir per classroom.
    coord_dir = Path.home() / ".axi" / "coordinator"
    materials = ClassroomMaterialsStore(
        coord_dir / "classrooms" / args.classroom_id
    )

    docs: list[dict] = []
    newly_added: list = []
    if args.upload:
        for upload_path in args.upload:
            p = Path(upload_path)
            if not p.exists():
                ui.emit_error(f"File not found: {upload_path}")
                return 1
            content = p.read_bytes()
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = ""  # binary file — still indexable by filename/title
            entry = materials.add_file(p)
            newly_added.append(entry)
            docs.append({"text": text, "source": p.name})

    # Preview still uses the in-memory stub for now. A future PR can swap
    # in a real retriever reading from materials + an embedding store.
    course_wf.indexer = _StubIndexer()
    course_wf.retriever = _StubRetriever(docs)

    if docs:
        course_wf.upload_corpus(docs)

    if newly_added:
        n = len(newly_added)
        ui.emit_success(
            f"Added {n} file{'' if n == 1 else 's'} to class "
            f"\"{args.classroom_id}\" materials."
        )

    if args.preview:
        results = course_wf.preview_corpus(args.preview)
        ui.out().print()
        ui.out().print(
            f"[dim]Preview for[/] [bold]\"{args.preview}\"[/] "
            f"[dim]→ {len(results)} result{'' if len(results) == 1 else 's'}:[/]"
        )
        for r in results[:5]:
            snippet = r.get("text", "")[:120]
            ui.out().print(f"  [cyan]{r.get('source')}[/] {snippet}")

    _save_course(course_wf, slug=course_data["slug"], title=course_data.get("title"))
    # Self-heal: if corpus completion brings the course to "publishable"
    # state, propagate course_selected to the classroom checklist now
    # rather than waiting for a manual re-approval of the prompt.
    _maybe_select_course_now(
        classroom_wf, classroom_data, course_wf, course_data,
    )
    _print_course_state(course_wf, course_data)
    return 0


def _cmd_prompt(args: argparse.Namespace) -> int:
    if not _require_active(args.classroom_id):
        return 1
    loaded = _load_classroom_and_course(args.classroom_id)
    if loaded is None:
        return 1
    classroom_wf, classroom_data, course_wf, course_data = loaded

    course_wf.llm = _stub_llm

    # Run set+test first if both are present, so `--set X --test Y --approve`
    # works as a single invocation. Without this, --approve short-circuits
    # before the prompt is staged and the workflow rejects it.
    if args.set is not None and args.test is not None:
        response = course_wf.test_prompt(args.set, args.test)
        print(f"\nTest response:\n  {response}\n")
        if not args.approve:
            print("Review the response. If acceptable, run:")
            print(f"  axi classroom prep prompt {args.classroom_id} --approve")

    if args.approve:
        try:
            course_wf.approve_prompt()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        _save_course(course_wf, slug=course_data["slug"], title=course_data.get("title"))
        # Once course prompt approved, auto-select course in classroom
        course_ready, _ = course_wf.is_ready_to_publish()
        if course_ready:
            classroom_wf.select_course(
                course_id=course_wf.course_id,
                course_version=(course_wf.manifest or {}).get("version", "1.0.0"),
                publishable=True,
                system_prompt=course_wf.system_prompt,
            )
            _save_classroom(
                classroom_wf, slug=classroom_data["slug"],
                title=classroom_data.get("title"),
                course_id=classroom_data["course_id"],
                course_slug=course_data["slug"],
            )
        _print_course_state(course_wf, course_data)
        return 0

    if args.set is None or args.test is None:
        print("prompt requires --set <text> and --test <query> (or --approve)",
              file=sys.stderr)
        return 1

    _save_course(course_wf, slug=course_data["slug"], title=course_data.get("title"))
    _print_course_state(course_wf, course_data)
    return 0


def _cmd_assessment(args: argparse.Namespace) -> int:
    loaded = _load_classroom_and_course(args.classroom_id)
    if loaded is None:
        return 1
    _, _, course_wf, course_data = loaded

    if args.skip:
        course_wf.skip_assessments()
    elif args.add:
        for entry in args.add:
            aid, _, atype = entry.partition(":")
            course_wf.define_assessment({"id": aid, "type": atype or "quiz"})
    else:
        print("Use --add <id[:type]> or --skip", file=sys.stderr)
        return 1

    _save_course(course_wf, slug=course_data["slug"], title=course_data.get("title"))
    _print_course_state(course_wf, course_data)
    return 0


def _cmd_rails(args: argparse.Namespace) -> int:
    loaded = _load_classroom_and_course(args.classroom_id)
    if loaded is None:
        return 1
    _, _, course_wf, course_data = loaded

    if args.use_defaults:
        course_wf.use_default_rails()
    elif args.config:
        try:
            rails_data = json.loads(Path(args.config).read_text())
        except Exception as e:
            print(f"Failed to load rails config: {e}", file=sys.stderr)
            return 1
        rails = rails_data if isinstance(rails_data, list) else rails_data.get("rails", [])
        course_wf.configure_rails(rails)
    else:
        print("Use --config <file.json> or --use-defaults", file=sys.stderr)
        return 1

    _save_course(course_wf, slug=course_data["slug"], title=course_data.get("title"))
    _print_course_state(course_wf, course_data)
    return 0


# --- Classroom-instance-step commands ---------------------------------------


def _cmd_rag(args: argparse.Namespace) -> int:
    loaded = _load_classroom(args.classroom_id)
    if loaded is None:
        print(f"No classroom '{args.classroom_id}'", file=sys.stderr)
        return 1
    classroom_wf, classroom_data = loaded

    try:
        classroom_wf.select_rag_policy(args.mode)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    _save_classroom(
        classroom_wf, slug=classroom_data["slug"],
        title=classroom_data.get("title"),
        course_id=classroom_data["course_id"],
        course_slug=classroom_data["course_slug"],
    )
    _print_classroom_state(classroom_wf, classroom_data)
    return 0


def _cmd_lms(args: argparse.Namespace) -> int:
    loaded = _load_classroom(args.classroom_id)
    if loaded is None:
        print(f"No classroom '{args.classroom_id}'", file=sys.stderr)
        return 1
    classroom_wf, classroom_data = loaded

    if args.fake:
        if args.fake_offline:
            classroom_wf.lms = _StubLMSOffline(roster_size=args.fake_roster)
        else:
            classroom_wf.lms = _StubLMS(roster_size=args.fake_roster)
    else:
        print(
            "Live Canvas integration not yet wired; use --fake for offline simulation.",
            file=sys.stderr,
        )
        return 1

    classroom_wf.connect_lms(course_id=args.canvas_course)
    _save_classroom(
        classroom_wf, slug=classroom_data["slug"],
        title=classroom_data.get("title"),
        course_id=classroom_data["course_id"],
        course_slug=classroom_data["course_slug"],
    )
    _print_classroom_state(classroom_wf, classroom_data)
    return 0


def _cmd_dry_run(args: argparse.Namespace) -> int:
    loaded = _load_classroom_and_course(args.classroom_id)
    if loaded is None:
        return 1
    classroom_wf, classroom_data, course_wf, course_data = loaded

    classroom_wf.llm = _stub_llm
    classroom_wf.retriever = _StubRetriever([])

    queries = args.query or [
        "What will I learn in this course?",
        "Describe the prerequisites.",
    ]
    from . import ui
    try:
        result = classroom_wf.run_dry_run(sample_queries=queries)
    except RuntimeError as e:
        ui.emit_error(str(e))
        return 1

    ui.emit_info(f"Dry run — {result.turns} turn(s).")
    for i, turn in enumerate(result.transcript, 1):
        ui.out().print()
        ui.out().print(f"  [bold][{i}] Q:[/] {turn['query']}")
        ui.out().print(f"  [bold]A:[/] {turn['response']}")

    _save_classroom(
        classroom_wf, slug=classroom_data["slug"],
        title=classroom_data.get("title"),
        course_id=classroom_data["course_id"],
        course_slug=classroom_data["course_slug"],
    )
    _print_classroom_state(classroom_wf, classroom_data)
    return 0


# ---------------------------------------------------------------------------
# Printers
# ---------------------------------------------------------------------------


_STATUS_GLYPH = {"completed": "✓", "failed": "✗", "warning": "!", "pending": "·"}


def _print_course_state(course_wf: CoursePrepWorkflow, course_data: dict) -> None:
    from . import ui
    ready, blockers = course_wf.is_ready_to_publish()
    version = (course_wf.manifest or {}).get("version", "1.0.0")
    ui.emit_checklist(
        title=f"Course \"{course_data['slug']}\"",
        subtitle=f"version {version}",
        steps=list(course_wf.checklist.steps),
        ready=ready,
        ready_message="Course is publishable.",
        blockers=blockers,
    )


def _print_classroom_state(
    classroom_wf: ClassroomPrepWorkflow, classroom_data: dict
) -> None:
    from . import ui
    ready, blockers = classroom_wf.is_ready_for_enrollment()
    course_slug = classroom_data["course_slug"]
    classroom_id = classroom_data.get("id") or classroom_data.get("slug", "<id>")
    ui.emit_checklist(
        title=f"Classroom \"{classroom_data['slug']}\"",
        subtitle=f"instance of course \"{course_slug}\"",
        steps=list(classroom_wf.checklist.steps),
        ready=ready,
        ready_message="Classroom is ready for enrollment.",
        blockers=blockers,
        next_command=(
            f"axi classroom publish {classroom_id} --approver <you>"
            if ready else None
        ),
    )


def _print_unified_summary(
    course_wf: CoursePrepWorkflow,
    course_data: dict,
    classroom_wf: ClassroomPrepWorkflow,
    classroom_data: dict,
) -> None:
    from . import ui
    _print_course_state(course_wf, course_data)
    _print_classroom_state(classroom_wf, classroom_data)

    course_ready, _ = course_wf.is_ready_to_publish()
    class_ready, _ = classroom_wf.is_ready_for_enrollment()
    if course_ready and class_ready:
        ui.emit_success("Course publishable, classroom ready for enrollment.")
    elif not course_ready:
        ui.emit_info("Next: finish course prep (corpus → prompt → approve).")
    else:
        ui.emit_info("Course is publishable; finish classroom prep (rag, lms).")


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi classroom",
        description="Classroom lifecycle: prep, create, enroll, status.",
    )
    # required=False so bare `axi classroom` prints a friendly
    # orientation instead of an argparse error. The handler in main()
    # falls through to _emit_orientation when no subcommand is given.
    sub = parser.add_subparsers(dest="command", required=False)

    prep = sub.add_parser("prep", help="Instructor preparation flow.")
    prep_sub = prep.add_subparsers(dest="prep_command", required=True)

    # init — no positional id, auto-generates
    p_init = prep_sub.add_parser(
        "init", help="Start a new classroom (auto-generates ids).",
    )
    p_init.add_argument("--title", default=None,
                        help="Optional title; used to derive a human-readable slug")
    p_init.add_argument("--instructor", required=True,
                        help="Instructor email or principal name")
    p_init.add_argument("--from", dest="from_ref", default=None,
                        help="Reuse an existing classroom or course (id or slug)")
    p_init.add_argument("--manifest", default=None,
                        help="Load a course manifest file (yaml/json) at init time")
    p_init.add_argument("--lms-assist-fake", action="store_true", default=False,
                        help="Use a simulated LMS import to pre-fill corpus, "
                             "assessments, and roster (demo/test). Real Canvas "
                             "integration comes via --lms when configured.")
    p_init.set_defaults(handler=_cmd_init)

    # status
    p_status = prep_sub.add_parser("status", help="Show classroom + course prep state.")
    p_status.add_argument("classroom_id", nargs="?", default="")
    p_status.add_argument(
        "--tmux", action="store_true",
        help="One-line tmux status-right output (for #(axi classroom "
             "prep status --tmux) embedding)",
    )
    p_status.set_defaults(handler=_cmd_status)

    # Attach argcomplete completers to classroom_id / course_id args so
    # tab completion surfaces live runtime IDs. Falls back silently if
    # argcomplete is absent.
    try:
        from axiom.extensions.builtins.classroom.status_line import (
            classroom_id_completer,
        )

        for _p in (p_status,):
            for action in _p._actions:
                if getattr(action, "dest", "") == "classroom_id":
                    action.completer = classroom_id_completer  # type: ignore[attr-defined]
    except Exception:
        pass

    # corpus (course-step)
    p_corpus = prep_sub.add_parser("corpus", help="Upload corpus + preview retrieval.")
    p_corpus.add_argument("classroom_id")
    p_corpus.add_argument("--upload", action="append", default=None)
    p_corpus.add_argument("--preview", default=None)
    p_corpus.set_defaults(handler=_cmd_corpus)

    # prompt (course-step)
    p_prompt = prep_sub.add_parser("prompt", help="Set + test + approve system prompt.")
    p_prompt.add_argument("classroom_id")
    p_prompt.add_argument("--set", default=None)
    p_prompt.add_argument("--test", default=None)
    p_prompt.add_argument("--approve", action="store_true", default=False)
    p_prompt.set_defaults(handler=_cmd_prompt)

    # tune-prompt — CLI parity for the classroom_prep_tune_prompt chat tool.
    # One-shot: set + test against a query + persist. Works at the course
    # level (prompts attach to courses, not classrooms).
    p_tune = prep_sub.add_parser(
        "tune-prompt",
        help=(
            "Set + test + save a system prompt in one call (CLI parity for "
            "the classroom_prep_tune_prompt chat tool)."
        ),
    )
    p_tune.add_argument(
        "course_id", help="Course id (NOT classroom id — prompts attach to courses).",
    )
    p_tune.add_argument(
        "--system-prompt", required=True, dest="system_prompt",
        help="The full system prompt to set.",
    )
    p_tune.add_argument(
        "--test-query", required=True, dest="test_query",
        help="Sample student query used to test the prompt end-to-end.",
    )
    p_tune.add_argument("--json", action="store_true", default=False)
    p_tune.set_defaults(handler=_cmd_prep_tune_prompt)

    # assessment (course-step)
    p_asm = prep_sub.add_parser("assessment", help="Add or skip assessments.")
    p_asm.add_argument("classroom_id")
    p_asm.add_argument("--add", action="append", default=None,
                       help="id[:type], repeatable")
    p_asm.add_argument("--skip", action="store_true", default=False)
    p_asm.set_defaults(handler=_cmd_assessment)

    # rails (course-step) — legacy signature kept for back-compat
    p_rails = prep_sub.add_parser("rails", help="Configure onboarding rails.")
    rails_sub = p_rails.add_subparsers(dest="rails_action")

    # Legacy inline args — when no subcommand, the old classroom-id path runs.
    p_rails.add_argument("classroom_id", nargs="?", default=None)
    p_rails.add_argument("--config", default=None,
                         help="Path to rails JSON file")
    p_rails.add_argument("--use-defaults", action="store_true", default=False)
    p_rails.set_defaults(handler=_cmd_rails)

    # New: list-banks / add / preview (P3b)
    p_list_banks = rails_sub.add_parser(
        "list-banks",
        help="List installed question banks (axiom-core + extensions).",
    )
    p_list_banks.add_argument("--json", action="store_true", default=False)
    p_list_banks.set_defaults(handler=_cmd_rails_list_banks)

    p_rails_add = rails_sub.add_parser(
        "add",
        help="Add a rail to a course, seeded from a question bank.",
    )
    p_rails_add.add_argument(
        "course_id", help="Course id (NOT classroom id — rails attach to a course).",
    )
    p_rails_add.add_argument("--rail-id", required=True, dest="rail_id")
    p_rails_add.add_argument("--bank", required=True, dest="bank_id")
    p_rails_add.add_argument(
        "--ids",
        default="",
        help=(
            "Optional comma-separated list of question ids from the bank "
            "to include (default: all)."
        ),
    )
    p_rails_add.add_argument(
        "--auto-apply-to",
        default="all_new_students",
        dest="auto_apply_to",
    )
    p_rails_add.add_argument(
        "--not-required",
        action="store_true",
        default=False,
        help="Mark the rail as optional (default: required).",
    )
    p_rails_add.add_argument("--json", action="store_true", default=False)
    p_rails_add.set_defaults(handler=_cmd_rails_add)

    p_rails_edit = rails_sub.add_parser(
        "edit",
        help="Edit a rail's YAML in $EDITOR (Track 5).",
    )
    p_rails_edit.add_argument(
        "course_id", help="Course id (NOT classroom id — rails attach to a course).",
    )
    p_rails_edit.add_argument("--rail-id", required=True, dest="rail_id")
    p_rails_edit.add_argument("--json", action="store_true", default=False)
    p_rails_edit.set_defaults(handler=_cmd_rails_edit)

    p_rails_preview = rails_sub.add_parser(
        "preview",
        help="Preview a rail as a stub student (@alice:demo).",
    )
    p_rails_preview.add_argument(
        "course_id", help="Course id (NOT classroom id).",
    )
    p_rails_preview.add_argument("--rail-id", required=True, dest="rail_id")
    p_rails_preview.add_argument("--json", action="store_true", default=False)
    p_rails_preview.set_defaults(handler=_cmd_rails_preview)

    # rag (classroom-step)
    p_rag = prep_sub.add_parser("rag", help="Select RAG policy mode.")
    p_rag.add_argument("classroom_id")
    p_rag.add_argument("--mode", required=True,
                       help="course_only | course_plus_institutional | full | ab_test | custom")
    p_rag.set_defaults(handler=_cmd_rag)

    # lms (classroom-step)
    p_lms = prep_sub.add_parser("lms", help="Connect LMS + preview roster.")
    p_lms.add_argument("classroom_id")
    p_lms.add_argument("--canvas-course", required=True)
    p_lms.add_argument("--fake", action="store_true", default=False)
    p_lms.add_argument("--fake-offline", action="store_true", default=False)
    p_lms.add_argument("--fake-roster", type=int, default=10)
    p_lms.set_defaults(handler=_cmd_lms)

    # dry-run (classroom-step)
    p_dry = prep_sub.add_parser("dry-run", help="Dry-run as a test student.")
    p_dry.add_argument("classroom_id")
    p_dry.add_argument("--query", action="append", default=None)
    p_dry.set_defaults(handler=_cmd_dry_run)

    # dry-run-enhanced (FW-1 P5) — retrieves from the real course corpus
    p_dry_en = prep_sub.add_parser(
        "dry-run-enhanced",
        help=(
            "Polished dry-run: retrieves from the course's actual corpus "
            "and shows grounded sample responses before publishing."
        ),
    )
    p_dry_en.add_argument("classroom_id")
    p_dry_en.add_argument("--query", action="append", default=None)
    p_dry_en.add_argument("--json", action="store_true", default=False)
    p_dry_en.set_defaults(handler=_cmd_dry_run_enhanced)

    # wrap (top-level) — FW-4 — post-course cohort operations
    p_wrap = sub.add_parser(
        "wrap",
        help="Post-course operations (analytics, harvest, grades, template).",
    )
    wrap_sub = p_wrap.add_subparsers(dest="wrap_action", required=True)

    # wrap analytics
    p_wa = wrap_sub.add_parser(
        "analytics", help="Cohort summary — roster, config, grade distributions.",
    )
    p_wa.add_argument("classroom_id")
    p_wa.add_argument("--json", action="store_true", default=False)
    p_wa.set_defaults(handler=_cmd_wrap_analytics)

    # wrap harvest — FW-4 P3
    p_wh = wrap_sub.add_parser(
        "harvest",
        help="Bundle anonymized cohort data into a .axiompack for research export.",
    )
    p_wh.add_argument("classroom_id")
    p_wh.add_argument(
        "--out", required=True, help="Output path for the .axiompack file.",
    )
    p_wh.add_argument("--json", action="store_true", default=False)
    p_wh.set_defaults(handler=_cmd_wrap_harvest)

    # wrap template — FW-4 P5 — propose updated CourseManifest
    p_wt = wrap_sub.add_parser(
        "template",
        help=(
            "Propose an updated CourseManifest for the next cohort based "
            "on this cohort's outcomes (advisory only)."
        ),
    )
    p_wt.add_argument("classroom_id")
    p_wt.add_argument(
        "--out", default="",
        help="Optional path to write the proposed manifest YAML.",
    )
    p_wt.add_argument("--json", action="store_true", default=False)
    p_wt.set_defaults(handler=_cmd_wrap_template)

    # wrap grades — FW-4 P4 — compute final grades + optionally push to Canvas
    p_wg = wrap_sub.add_parser(
        "grades",
        help=(
            "Compute per-student final grades from the grade ledger. "
            "Add --push to send to Canvas."
        ),
    )
    p_wg.add_argument("classroom_id")
    p_wg.add_argument(
        "--push", action="store_true", default=False,
        help="Actually push to Canvas (default: compute-only).",
    )
    p_wg.add_argument(
        "--canvas-course-id", default="", dest="canvas_course_id",
        help="Target Canvas course id (defaults to classroom's lms_course_id).",
    )
    p_wg.add_argument(
        "--canvas-assignment-id", default="", dest="canvas_assignment_id",
        help="Target Canvas assignment id (required when --push).",
    )
    p_wg.add_argument("--json", action="store_true", default=False)
    p_wg.set_defaults(handler=_cmd_wrap_grades)

    # enroll (top-level) — Keplo P6a — student batch enrollment
    p_enroll = sub.add_parser(
        "enroll",
        help=(
            "Enroll students into a published classroom (generates tokens,"
            " nationality attestations, rail checklists)."
        ),
    )
    p_enroll.add_argument("classroom_id")
    p_enroll.add_argument(
        "--instructor", required=True,
        help="Instructor principal signing the attestations (e.g. @ben:ut).",
    )
    p_enroll.add_argument(
        "--fake", action="store_true", default=False,
        help="Use populated CanvasMockServer (demo + testing).",
    )
    p_enroll.add_argument(
        "--ttl-days", type=int, default=30, dest="ttl_days",
        help="Token lifetime (default: 30).",
    )
    p_enroll.add_argument(
        "--canvas-course-id", default="", dest="canvas_course_id",
        help="Override Canvas course id (defaults to classroom's lms_course_id).",
    )
    p_enroll.add_argument("--json", action="store_true", default=False)
    p_enroll.set_defaults(handler=_cmd_enroll)

    # join — the student-facing command for entering a classroom.
    # Internal detail: resolves coordinator URL from the invite's embedded
    # field or the --coordinator flag; auto-initializes a node identity if
    # missing; posts a signed join request and stores the returned
    # membership. All of that is kept out of the help text — students only
    # need to know "paste the invite your instructor sent."
    p_join = sub.add_parser(
        "join",
        help=(
            "Join a classroom using the invite your instructor sent you."
        ),
    )
    p_join.add_argument(
        "invite",
        help="The invite your instructor sent you.",
    )
    p_join.add_argument(
        "--coordinator",
        default=None,
        help=(
            "Classroom server URL. Most invites include this — you only "
            "need this if your instructor asked you to paste a URL "
            "separately."
        ),
    )
    p_join.add_argument(
        "--student-id",
        default=None,
        help=(
            "The name your instructor used when they enrolled you. "
            "Defaults to your email."
        ),
    )
    p_join.add_argument("--json", action="store_true", default=False)
    p_join.set_defaults(handler=_cmd_join)

    # invite (top-level) — instructor mints a copy-paste invite for a student
    p_invite = sub.add_parser(
        "invite",
        help="Create an invite to send to a student.",
    )
    p_invite.add_argument(
        "classroom_id",
        help="The class you're inviting the student to (e.g. NE101).",
    )
    p_invite.add_argument(
        "--coordinator-url",
        default=None,
        help=(
            "Public URL of your classroom server. You only need to type "
            "this once per class — it's remembered for future invites."
        ),
    )
    p_invite.add_argument(
        "--ttl-hours",
        type=int,
        default=168,  # 7 days — a student should have time to open their laptop.
        help="How long the invite stays valid, in hours. Default: 168 (7 days).",
    )
    p_invite.add_argument(
        "--count",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Mint N single-use invites in one call. Each invite is "
            "independent — pass one per student. Default: 1."
        ),
    )
    p_invite.add_argument("--json", action="store_true", default=False)
    p_invite.set_defaults(handler=_cmd_invite)

    # serve (top-level) — instructor runs the long-running coordinator server
    p_serve = sub.add_parser(
        "serve",
        help="Run the classroom server so students can join.",
    )
    p_serve.add_argument(
        "classroom_id",
        help="Which class this server is for (e.g. NE101).",
    )
    p_serve.add_argument(
        "--host", default="127.0.0.1",
        help="Network address to bind to. Default: 127.0.0.1 (this machine only).",
    )
    p_serve.add_argument(
        "--port", type=int, default=8787,
        help="Port to listen on. Default: 8787.",
    )
    p_serve.set_defaults(handler=_cmd_serve)

    # modes (top-level) — instructor manages the classroom's mode policy
    p_modes = sub.add_parser(
        "modes",
        help="Show or change which learning modes students may use.",
    )
    p_modes.add_argument(
        "classroom_id",
        help="Which class to configure (e.g. NE101).",
    )
    p_modes.add_argument(
        "--allow",
        default=None,
        help=(
            "Comma-separated list of allowed modes — overrides the "
            "current set. Use 'all' to reset to everything enabled."
        ),
    )
    p_modes.add_argument(
        "--force",
        default=None,
        help=(
            "Force a specific mode for all students (e.g. 'quiz' for "
            "quiz week). Use 'none' to clear a prior force."
        ),
    )
    p_modes.add_argument("--json", action="store_true", default=False)
    p_modes.set_defaults(handler=_cmd_modes)

    # briefs (top-level) — instructor manages per-student briefs
    p_briefs = sub.add_parser(
        "briefs",
        help="Generate, review, and approve per-student learning briefs.",
    )
    briefs_sub = p_briefs.add_subparsers(dest="briefs_action", required=True)

    p_briefs_gen = briefs_sub.add_parser(
        "generate",
        help="Generate fresh briefs for every student in a class.",
    )
    p_briefs_gen.add_argument("classroom_id")
    p_briefs_gen.add_argument("--json", action="store_true", default=False)
    p_briefs_gen.set_defaults(handler=_cmd_briefs_generate)

    p_briefs_rev = briefs_sub.add_parser(
        "review",
        help="Review a student's latest brief; optionally add a note + approve.",
    )
    p_briefs_rev.add_argument("classroom_id")
    p_briefs_rev.add_argument("student_id")
    p_briefs_rev.add_argument(
        "--note", default=None,
        help="Instructor note to attach to the brief.",
    )
    p_briefs_rev.add_argument(
        "--approve", action="store_true", default=False,
        help="Mark the brief as approved (student will see it on `me`).",
    )
    p_briefs_rev.add_argument("--json", action="store_true", default=False)
    p_briefs_rev.set_defaults(handler=_cmd_briefs_review)

    p_briefs_list = briefs_sub.add_parser(
        "list",
        help="List all students with pending (draft) briefs to review.",
    )
    p_briefs_list.add_argument("classroom_id")
    p_briefs_list.add_argument("--json", action="store_true", default=False)
    p_briefs_list.set_defaults(handler=_cmd_briefs_list)

    # me (top-level) — student views their own approved brief
    p_me = sub.add_parser(
        "me",
        help="See your latest approved learning brief for a class.",
    )
    p_me.add_argument("classroom_id")
    p_me.add_argument(
        "--memory",
        action="store_true",
        default=False,
        help="Show what the coordinator has logged about your activity "
             "(question count, modes used, recent topics) — memory "
             "transparency for the student.",
    )
    p_me.add_argument(
        "--forget",
        metavar="INTERACTION_ID",
        default=None,
        help="Retract a specific interaction so it stops surfacing in "
             "the coordinator's memory of you. Look up the id via "
             "`axi classroom me <CID> --memory`.",
    )
    p_me.add_argument("--json", action="store_true", default=False)
    p_me.set_defaults(handler=_cmd_me)

    # ask-instructor — student opens a thread with the instructor
    p_ai = sub.add_parser(
        "ask-instructor",
        help="Ask your instructor a question (creates a thread).",
    )
    p_ai.add_argument("classroom_id")
    p_ai.add_argument("message", help="What you want to ask.")
    p_ai.add_argument("--json", action="store_true", default=False)
    p_ai.set_defaults(handler=_cmd_ask_instructor)

    # ask-student — instructor opens a thread with a specific student
    p_as = sub.add_parser(
        "ask-student",
        help="Ask a specific student a follow-up question (creates a thread).",
    )
    p_as.add_argument("classroom_id")
    p_as.add_argument("student_id")
    p_as.add_argument("message", help="Your question for the student.")
    p_as.add_argument("--json", action="store_true", default=False)
    p_as.set_defaults(handler=_cmd_ask_student)

    # threads — list threads (role-aware: instructor sees all, student sees their own)
    p_threads = sub.add_parser(
        "threads",
        help="List conversation threads for a class.",
    )
    p_threads.add_argument("classroom_id")
    p_threads.add_argument(
        "--open-only", action="store_true", default=False,
        help="Show only threads still awaiting a response.",
    )
    p_threads.add_argument("--json", action="store_true", default=False)
    p_threads.set_defaults(handler=_cmd_threads)

    # reply — either side replies to an existing thread
    p_reply = sub.add_parser(
        "reply",
        help="Reply to a thread.",
    )
    p_reply.add_argument("classroom_id")
    p_reply.add_argument("thread_id")
    p_reply.add_argument("message")
    p_reply.add_argument("--json", action="store_true", default=False)
    p_reply.set_defaults(handler=_cmd_reply)

    # quiz (top-level) — broadcast quiz lifecycle
    p_quiz = sub.add_parser(
        "quiz",
        help="Broadcast a quiz to the cohort (instructor) or take one (student).",
    )
    quiz_sub = p_quiz.add_subparsers(dest="quiz_action", required=True)

    p_qb = quiz_sub.add_parser(
        "broadcast",
        help="Instructor: create a quiz from a bank and push to all students.",
    )
    p_qb.add_argument("classroom_id")
    p_qb.add_argument(
        "--bank-preset",
        default="ne101-core",
        help="Which shipped bank to draw from. 'list' to see available.",
    )
    p_qb.add_argument(
        "--category",
        default=None,
        help="Only pick questions with this category (optional).",
    )
    p_qb.add_argument(
        "--questions", type=int, default=3,
        help="How many questions to include (default: 3).",
    )
    p_qb.add_argument(
        "--topic", default=None,
        help="Label for the quiz — shown to students.",
    )
    p_qb.add_argument("--json", action="store_true", default=False)
    p_qb.set_defaults(handler=_cmd_quiz_broadcast)

    p_qp = quiz_sub.add_parser(
        "pending",
        help="Student: list quizzes your instructor pushed that you haven't taken.",
    )
    p_qp.add_argument("classroom_id")
    p_qp.add_argument("--json", action="store_true", default=False)
    p_qp.set_defaults(handler=_cmd_quiz_pending)

    p_qt = quiz_sub.add_parser(
        "take",
        help="Student: take a broadcast quiz interactively.",
    )
    p_qt.add_argument("classroom_id")
    p_qt.add_argument("quiz_id")
    p_qt.add_argument("--json", action="store_true", default=False)
    p_qt.set_defaults(handler=_cmd_quiz_take)

    p_qr = quiz_sub.add_parser(
        "results",
        help="Instructor: see the submissions + scores for a quiz.",
    )
    p_qr.add_argument("classroom_id")
    p_qr.add_argument("quiz_id")
    p_qr.add_argument("--json", action="store_true", default=False)
    p_qr.set_defaults(handler=_cmd_quiz_results)

    # status (top-level) — instructor dashboard: who's in which class
    p_status_cmd = sub.add_parser(
        "status",
        help="Show classes you're running and who has joined.",
    )
    p_status_cmd.add_argument(
        "classroom_id",
        nargs="?",
        default=None,
        help="Optional class to drill down on. Omit to see all classes.",
    )
    p_status_cmd.add_argument("--json", action="store_true", default=False)
    p_status_cmd.set_defaults(handler=_cmd_classroom_status)

    # ask (top-level) — Q&A grounded in the student's downloaded materials
    p_ask = sub.add_parser(
        "ask",
        help="Ask a question about your class materials.",
    )
    p_ask.add_argument(
        "classroom_id",
        help="Which class to search (e.g. NE101).",
    )
    p_ask.add_argument(
        "question",
        help="The question you want answered, in your own words.",
    )
    p_ask.add_argument(
        "-k", type=int, default=3,
        help="How many citations to show (default: 3).",
    )
    p_ask.add_argument(
        "--cite-only", action="store_true", default=False,
        help="Skip the grounded-answer synthesis. Cheaper + fully offline.",
    )
    p_ask.add_argument(
        "--mode",
        default=None,
        help=(
            "Learning mode: ask (default), tutor (Socratic), review, "
            "reflect, quiz. Your pick is bounded by your instructor's "
            "classroom policy."
        ),
    )
    p_ask.add_argument("--json", action="store_true", default=False)
    p_ask.set_defaults(handler=_cmd_ask)

    # evals (top-level) — instructor runs a question bank against the class pipeline
    p_evals = sub.add_parser(
        "evals",
        help="Run a question bank against the class Q&A pipeline.",
    )
    p_evals.add_argument(
        "classroom_id",
        help="Which class to evaluate (must have a local index).",
    )
    bank_src = p_evals.add_mutually_exclusive_group(required=True)
    bank_src.add_argument(
        "--bank",
        help="Path to a JSONL question bank.",
    )
    bank_src.add_argument(
        "--bank-preset",
        help=(
            "Name of a bank shipped with the extension "
            "(e.g. 'ne101-core'). Use '--bank-preset list' to see "
            "what's available."
        ),
    )
    p_evals.add_argument(
        "-k", type=int, default=3,
        help="Citations retrieved per question (default: 3).",
    )
    p_evals.add_argument(
        "--cite-only", action="store_true", default=False,
        help="Skip LLM synthesis — score the citations-only output.",
    )
    p_evals.add_argument(
        "--baseline", action="store_true", default=False,
        help=(
            "Also run each question through the LLM with NO class "
            "materials, to measure the retrieval lift."
        ),
    )
    p_evals.add_argument(
        "--min-pass-rate",
        type=float,
        default=None,
        help=(
            "CI gate — exit non-zero if the Axiom-pipeline pass rate "
            "drops below this value (e.g. 0.7 for 70%%). Without this "
            "flag the command exits 1 only if ANY question fails."
        ),
    )
    p_evals.add_argument("--json", action="store_true", default=False)
    p_evals.set_defaults(handler=_cmd_evals)

    # archive (top-level) — FW-4 P1 — terminal lifecycle transition
    p_archive = sub.add_parser(
        "archive",
        help=(
            "Archive a completed classroom (published → archived). "
            "Terminal state; republish is refused. Clone into a new "
            "classroom to start a fresh cohort."
        ),
    )
    p_archive.add_argument("classroom_id")
    p_archive.add_argument(
        "--archiver", required=True,
        help="Instructor principal archiving the classroom (e.g. @ben:ut).",
    )
    p_archive.add_argument(
        "--reason", required=True,
        help="Why the classroom is being archived (end of semester, etc.).",
    )
    p_archive.add_argument("--json", action="store_true", default=False)
    p_archive.set_defaults(handler=_cmd_archive)

    # doctor (top-level) — diagnose a classroom on this machine
    p_doctor = sub.add_parser(
        "doctor",
        help=(
            "Diagnose a classroom on this machine: identity, artifacts, "
            "materials, coordinator URL, server reachability. "
            "Read-only — surfaces what's wrong + the exact next command."
        ),
    )
    p_doctor.add_argument("classroom_id")
    p_doctor.add_argument("--json", action="store_true", default=False)
    p_doctor.set_defaults(handler=_cmd_doctor)

    # export (top-level) — bundle a classroom into a portable .tar.gz
    p_export = sub.add_parser(
        "export",
        help=(
            "Export a classroom to a portable .tar.gz bundle "
            "(end-of-semester keepsake, full fidelity, no anonymization)."
        ),
    )
    p_export.add_argument("classroom_id")
    p_export.add_argument(
        "--out", required=True,
        help="Path to write the .tar.gz bundle to.",
    )
    p_export.add_argument("--json", action="store_true", default=False)
    p_export.set_defaults(handler=_cmd_export)

    # leave (top-level) — student-side disconnect from a classroom
    p_leave = sub.add_parser(
        "leave",
        help=(
            "Leave a classroom (student): remove local cache, "
            "membership, and indexed materials."
        ),
    )
    p_leave.add_argument("classroom_id")
    p_leave.add_argument(
        "--keep-materials",
        action="store_true",
        default=False,
        help="Keep local indexed materials (default: remove them).",
    )
    p_leave.add_argument("--json", action="store_true", default=False)
    p_leave.set_defaults(handler=_cmd_leave)

    # publish (top-level) — transition a fully-prepped classroom to published
    p_publish = sub.add_parser(
        "publish",
        help=(
            "Publish a prepared classroom (prep → published). "
            "Requires both course and classroom checklists to be green."
        ),
    )
    p_publish.add_argument("classroom_id")
    p_publish.add_argument(
        "--approver", required=True,
        help="Instructor principal publishing the classroom (e.g. @ben:ut).",
    )
    p_publish.add_argument("--json", action="store_true", default=False)
    p_publish.set_defaults(handler=_cmd_publish)

    # lms-setup — guided LMS walkthrough (FW-1 P4)
    p_lms_setup = prep_sub.add_parser(
        "lms-setup",
        help="Guided LMS walkthrough (Canvas / Moodle / Blackboard / None).",
    )
    lms_sub = p_lms_setup.add_subparsers(dest="lms_action", required=True)

    p_lp = lms_sub.add_parser(
        "list-providers", help="Show LMS providers and their status.",
    )
    p_lp.add_argument("--json", action="store_true", default=False)
    p_lp.set_defaults(handler=_cmd_lms_setup_list_providers)

    p_cp = lms_sub.add_parser("canvas-probe", help="Test Canvas connectivity.")
    p_cp.add_argument("--instance-url", required=True, dest="instance_url")
    p_cp.add_argument("--token", required=True)
    p_cp.add_argument("--fake", action="store_true", default=False)
    p_cp.add_argument("--json", action="store_true", default=False)
    p_cp.set_defaults(handler=_cmd_lms_setup_canvas_probe)

    p_cc = lms_sub.add_parser(
        "canvas-configure", help="Wire Canvas to a classroom (probe + roster).",
    )
    p_cc.add_argument("classroom_id")
    p_cc.add_argument("--instance-url", required=True, dest="instance_url")
    p_cc.add_argument("--token", required=True)
    p_cc.add_argument(
        "--canvas-course-id", required=True, dest="canvas_course_id",
    )
    p_cc.add_argument("--fake", action="store_true", default=False)
    p_cc.add_argument("--json", action="store_true", default=False)
    p_cc.set_defaults(handler=_cmd_lms_setup_canvas_configure)

    p_none = lms_sub.add_parser("none", help="Mark classroom as no-LMS.")
    p_none.add_argument("classroom_id")
    p_none.add_argument("--json", action="store_true", default=False)
    p_none.set_defaults(handler=_cmd_lms_setup_none)

    # checkpoints — configure course checkpoints (FW-1 P3a)
    p_ck = prep_sub.add_parser(
        "checkpoints",
        help="Configure course checkpoints (baseline, midpoint, final, custom).",
    )
    ck_sub = p_ck.add_subparsers(dest="ck_action", required=True)

    p_ck_list = ck_sub.add_parser("list", help="List configured checkpoints.")
    p_ck_list.add_argument(
        "course_id", help="Course id (NOT classroom id — checkpoints attach to a course).",
    )
    p_ck_list.add_argument("--json", action="store_true", default=False)
    p_ck_list.set_defaults(handler=_cmd_checkpoints_list)

    p_ck_add = ck_sub.add_parser("add", help="Add (or update) a checkpoint.")
    p_ck_add.add_argument(
        "course_id", help="Course id (NOT classroom id).",
    )
    p_ck_add.add_argument("--id", required=True, dest="checkpoint_id")
    p_ck_add.add_argument(
        "--timing",
        required=True,
        help=(
            "Keyword (enrollment_complete | course_start | midway | "
            "course_end) OR ISO-8601 date (2026-07-15)."
        ),
    )
    p_ck_add.add_argument(
        "--method",
        default="quiz",
        choices=["quiz", "questionnaire", "portfolio", "observation", "none"],
    )
    p_ck_add.add_argument("--label", default="")
    p_ck_add.add_argument("--questionnaire-id", dest="questionnaire_id", default="")
    p_ck_add.add_argument("--required", action="store_true", default=False)
    p_ck_add.add_argument("--json", action="store_true", default=False)
    p_ck_add.set_defaults(handler=_cmd_checkpoints_add)

    p_ck_rm = ck_sub.add_parser("remove", help="Remove a checkpoint by id.")
    p_ck_rm.add_argument(
        "course_id", help="Course id (NOT classroom id).",
    )
    p_ck_rm.add_argument("--id", required=True, dest="checkpoint_id")
    p_ck_rm.add_argument("--json", action="store_true", default=False)
    p_ck_rm.set_defaults(handler=_cmd_checkpoints_remove)

    p_ck_skip = ck_sub.add_parser(
        "skip-defaults",
        help="Opt out of default checkpoints (empties the list).",
    )
    p_ck_skip.add_argument(
        "course_id", help="Course id (NOT classroom id).",
    )
    p_ck_skip.add_argument("--json", action="store_true", default=False)
    p_ck_skip.set_defaults(handler=_cmd_checkpoints_skip_defaults)

    # from-demo — clone the seeded demo course + classroom
    p_from_demo = prep_sub.add_parser(
        "from-demo",
        help=(
            "Clone the demo course AND classroom into new editable "
            "artifacts (FW-1 P1)."
        ),
    )
    p_from_demo.add_argument(
        "new_course_id",
        help="ID for the cloned course (must not collide with an existing course)",
    )
    p_from_demo.add_argument(
        "--instructor",
        required=True,
        help="Instructor principal (email or @handle) for the cloned course",
    )
    p_from_demo.add_argument(
        "--classroom-id",
        default="",
        dest="new_classroom_id",
        help=(
            "Optional classroom id for the cloned classroom. Defaults to the "
            "same value as new_course_id (course and classroom IDs are in "
            "separate namespaces)."
        ),
    )
    p_from_demo.add_argument("--json", action="store_true", default=False)
    p_from_demo.set_defaults(handler=_cmd_prep_from_demo)

    # demo — top-level: seed a fully-populated demo classroom
    p_demo = sub.add_parser(
        "demo",
        help="Seed a running demo classroom for skeptic-evaluation-in-60s.",
    )
    p_demo.add_argument(
        "--reset",
        action="store_true",
        default=False,
        help="Wipe existing demo artifacts before reseeding",
    )
    p_demo.add_argument("--json", action="store_true", default=False)
    p_demo.set_defaults(handler=_cmd_demo)

    # brief — CHALKE's instructor daily brief (#24)
    p_brief = sub.add_parser(
        "brief", help="CHALKE instructor brief: signals, stuck students, open tickets."
    )
    p_brief.add_argument("classroom_id")
    p_brief.add_argument("--instructor", required=True,
                         help="Instructor principal (email or @handle)")
    p_brief.add_argument("--format", default="text",
                         choices=["text", "json"],
                         help="Output format (default: text)")
    p_brief.set_defaults(handler=_cmd_brief)

    # explain — one-click grade provenance (#26)
    p_explain = sub.add_parser(
        "explain",
        help="Grade-explain: provenance trace of a student's scored question.",
    )
    p_explain.add_argument("classroom_id")
    p_explain.add_argument("--student", required=True)
    p_explain.add_argument("--assessment", required=True)
    p_explain.add_argument("--question", required=True)
    p_explain.add_argument("--format", default="markdown",
                           choices=["markdown", "json"])
    p_explain.set_defaults(handler=_cmd_explain)

    # compare — side-by-side student answers (#25)
    p_compare = sub.add_parser(
        "compare", help="Side-by-side student answer comparison."
    )
    p_compare.add_argument("classroom_id")
    p_compare.add_argument("--assessment", required=True)
    p_compare.add_argument("--question", required=True)
    p_compare.add_argument("--students", required=True,
                           help="Comma-separated student ids")
    p_compare.add_argument("--format", default="markdown",
                           choices=["markdown", "json"])
    p_compare.set_defaults(handler=_cmd_compare)

    # --- canvas pull -----------------------------------------------------
    p_canvas = sub.add_parser(
        "canvas",
        help="Canvas LMS integration (pull course content into materials store).",
    )
    canvas_sub = p_canvas.add_subparsers(dest="canvas_action", required=True)
    p_canvas_pull = canvas_sub.add_parser(
        "pull",
        help="Pull pages, announcements, files, and module outline from a Canvas course.",
    )
    p_canvas_pull.add_argument("classroom_id")
    p_canvas_pull.add_argument(
        "--canvas-course-id", required=True, help="Canvas course ID to fetch."
    )
    p_canvas_pull.add_argument(
        "--fake",
        action="store_true",
        help="Use the seeded fake Canvas mock (offline demo).",
    )
    p_canvas_pull.add_argument(
        "--canvas-url", default="", help="Canvas API base URL (live mode)."
    )
    p_canvas_pull.add_argument(
        "--canvas-token", default="", help="Canvas API token (live mode)."
    )
    p_canvas_pull.add_argument(
        "--json", action="store_true", help="Emit summary as JSON."
    )
    p_canvas_pull.set_defaults(handler=_cmd_canvas_pull)

    # --- proposals --------------------------------------------------------
    p_props = sub.add_parser(
        "proposals",
        help="LMS proposal queue — drafts → approve → push.",
    )
    props_sub = p_props.add_subparsers(dest="proposals_action", required=True)

    p_pc = props_sub.add_parser("create", help="Create a draft proposal.")
    p_pc.add_argument("classroom_id")
    p_pc.add_argument("--target", required=True, choices=["page", "announcement", "assignment", "module"])
    p_pc.add_argument("--target-id", default="")
    p_pc.add_argument("--action", required=True, choices=["create", "update"])
    p_pc.add_argument("--title", required=True)
    p_pc.add_argument("--body", required=True)
    p_pc.add_argument("--created-by", required=True)
    p_pc.set_defaults(handler=_cmd_proposals_create)

    p_pl = props_sub.add_parser("list", help="List proposals for a classroom.")
    p_pl.add_argument("classroom_id")
    p_pl.add_argument("--status", default="", help="Filter by status.")
    p_pl.add_argument("--json", action="store_true")
    p_pl.set_defaults(handler=_cmd_proposals_list)

    p_pa = props_sub.add_parser("approve", help="Approve a draft proposal.")
    p_pa.add_argument("proposal_id")
    p_pa.add_argument("--by", required=True, help="Principal approving the proposal.")
    p_pa.set_defaults(handler=_cmd_proposals_approve)

    p_pr = props_sub.add_parser("reject", help="Reject a proposal with reason.")
    p_pr.add_argument("proposal_id")
    p_pr.add_argument("--reason", required=True)
    p_pr.add_argument("--by", required=True)
    p_pr.set_defaults(handler=_cmd_proposals_reject)

    p_pp = props_sub.add_parser(
        "push", help="Push an approved proposal to the LMS."
    )
    p_pp.add_argument("proposal_id")
    p_pp.add_argument("--canvas-course-id", required=True)
    p_pp.add_argument("--fake", action="store_true")
    p_pp.add_argument("--canvas-url", default="")
    p_pp.add_argument("--canvas-token", default="")
    p_pp.set_defaults(handler=_cmd_proposals_push)

    return parser


# ---------------------------------------------------------------------------
# Brief / Explain / Compare handlers (CHALKE-backed classroom surfaces)
# ---------------------------------------------------------------------------


def _cmd_brief(args: argparse.Namespace) -> int:
    """CHALKE instructor daily brief.

    Fuses the existing stubbed signals (stuck_students etc. — future
    work) with a live interaction-store feed: question volume, hot
    topics, quiet students. The interaction feed is what Prague
    instructors actually need to see in week one; the stubbed
    signals layer in as the agent-team pieces land.
    """
    import json as _json

    from . import ui
    from .agents.chalke import Chalke
    from .classroom_interaction import (
        ClassroomInteractionStore,
        topic_histogram,
    )
    from .composition_boot import build_classroom_composition
    from .coordinator_cohort_store import CohortNotFoundError, FileCohortStore

    composition = build_classroom_composition(classroom_id=args.classroom_id)
    chalke = Chalke(classroom_id=args.classroom_id, composition=composition)
    brief = chalke.for_instructor().daily_brief(instructor_id=args.instructor)

    # Live signals from the coordinator interaction log.
    coord_dir = (
        Path.home() / ".axi" / "coordinator" / "classrooms" / args.classroom_id
    )
    interactions = ClassroomInteractionStore(coord_dir).list()
    total_questions = len(interactions)
    distinct_askers = len({r.student_id for r in interactions if r.student_id})
    unanswered_count = sum(1 for r in interactions if not r.had_answer)
    hot_topics = topic_histogram(interactions, top_n=5)

    # Mode usage across the cohort — lets the instructor see at a
    # glance whether students are mostly asking (passive), tutoring
    # (productive struggle), or quizzing (retrieval practice).
    from collections import Counter as _Counter
    mode_counts: dict[str, int] = dict(
        _Counter(r.mode or "ask" for r in interactions)
    )

    roster: list[str] = []
    try:
        cohort_store = FileCohortStore(Path.home() / ".axi" / "coordinator")
        cohort = cohort_store.load(args.classroom_id)
        roster = [m.student_id for m in cohort.members]
    except CohortNotFoundError:
        pass
    quiet_students = (
        ClassroomInteractionStore(coord_dir).quiet_students(roster=roster)
        if roster else []
    )

    if args.format == "json":
        brief["live_signals"] = {
            "total_questions": total_questions,
            "distinct_askers": distinct_askers,
            "unanswered_questions": unanswered_count,
            "hot_topics": [
                {"token": token, "count": count} for token, count in hot_topics
            ],
            "quiet_students": quiet_students,
            "mode_usage": mode_counts,
        }
        print(_json.dumps(brief, indent=2))
        return 0

    ui.emit_kv("Daily brief", {
        "Classroom": brief["classroom_id"],
        "Compiled": _friendly_expiry(brief["compiled_at"]),
    })
    tp = brief.get("top_priority")
    if tp:
        ui.out().print()
        ui.out().print(f"[bold red]Top priority:[/] {tp.get('kind')}")

    # Live feed first — that's the new value-add.
    ui.out().print()
    ui.out().print("[bold]Questions this period[/]")
    if total_questions == 0:
        ui.out().print(
            "  [dim]No questions logged yet. Students either haven't "
            "asked, or they're running `axi classroom ask --cite-only`.[/]"
        )
    else:
        ui.out().print(
            f"  [dim]Total:[/] {total_questions}  "
            f"[dim]from[/] {distinct_askers} "
            f"student{'' if distinct_askers == 1 else 's'}"
        )
        if unanswered_count:
            ui.emit_warn(
                f"{unanswered_count} question"
                f"{'' if unanswered_count == 1 else 's'} had no match "
                "in the class materials (add more corpus?)"
            )

    if hot_topics:
        ui.out().print()
        ui.out().print("[bold]Hot topics[/]")
        for token, count in hot_topics:
            ui.out().print(f"  [cyan]{token}[/]  [dim]× {count}[/]")

    if mode_counts:
        ui.out().print()
        ui.out().print("[bold]Mode usage[/]")
        # Sort by count desc so the most-used mode surfaces first.
        for mode_name, count in sorted(
            mode_counts.items(), key=lambda kv: -kv[1],
        ):
            ui.out().print(f"  [cyan]{mode_name}[/]  [dim]× {count}[/]")

    if quiet_students:
        ui.out().print()
        ui.out().print(
            f"[bold]Quiet students[/] [dim]({len(quiet_students)})[/]"
        )
        for sid in quiet_students[:10]:
            ui.out().print(f"  [dim]•[/] {sid}")

    # Legacy stubbed signals (will become real as agent team lands).
    def _section(label: str, items: list, style: str = "") -> None:
        if not items:
            return
        ui.out().print()
        ui.out().print(f"[bold]{label}[/] ({len(items)})")
        for it in items:
            sid = it.get("student_id", "?")
            detail = (
                it.get("topic") or it.get("misconception_id")
                or it.get("objective_id") or ""
            )
            ui.out().print(f"  [dim]•[/] {sid}  [dim]{detail}[/]")

    _section("Stuck students", brief.get("stuck_students", []))
    _section("Misconceptions", brief.get("misconceptions", []))
    _section("Low engagement", brief.get("low_engagement", []))
    _section("Objective gaps", brief.get("objective_gaps", []))

    n_open = brief.get("open_help_tickets", 0)
    ui.out().print()
    if n_open:
        ui.emit_warn(f"{n_open} open help ticket{'' if n_open == 1 else 's'}.")
    else:
        ui.emit_info("No open help tickets.")
    return 0


def _format_brief_text(brief: dict) -> str:
    lines = [
        f"Classroom: {brief['classroom_id']}",
        f"Compiled: {brief['compiled_at']}",
        "",
    ]
    tp = brief.get("top_priority")
    if tp:
        lines.append(f"TOP PRIORITY: {tp.get('kind')}")
        lines.append("")

    def _section(label, items):
        if items:
            lines.append(f"{label} ({len(items)}):")
            for it in items:
                sid = it.get("student_id", "?")
                detail = (
                    it.get("topic") or it.get("misconception_id")
                    or it.get("objective_id") or ""
                )
                lines.append(f"  - {sid} {detail}")
            lines.append("")

    _section("Stuck students", brief.get("stuck_students", []))
    _section("Misconceptions", brief.get("misconceptions", []))
    _section("Low engagement", brief.get("low_engagement", []))
    _section("Objective gaps", brief.get("objective_gaps", []))
    lines.append(f"Open help tickets: {brief.get('open_help_tickets', 0)}")
    return "\n".join(lines)


def _cmd_explain(args: argparse.Namespace) -> int:
    """One-click grade-explain for a (student, assessment, question)."""
    import json as _json

    from .composition_boot import build_classroom_composition
    from .grade_explain import explain_grade, render_markdown

    composition = build_classroom_composition(classroom_id=args.classroom_id)
    explanation = explain_grade(
        composition,
        student_id=args.student,
        assessment_id=args.assessment,
        question_id=args.question,
    )
    if args.format == "json":
        print(_json.dumps({
            "student_id": explanation.student_id,
            "assessment_id": explanation.assessment_id,
            "question_id": explanation.question_id,
            "score_fragment": explanation.score_fragment,
            "response_trace": explanation.response_trace,
            "override_events": explanation.override_events,
            "breach_events": explanation.breach_events,
            "audit_entries": explanation.audit_entries,
        }, indent=2))
    else:
        print(render_markdown(explanation))
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    """Side-by-side student answer comparison."""
    import json as _json

    from .compare_answers import compare_answers, render_markdown
    from .composition_boot import build_classroom_composition

    composition = build_classroom_composition(classroom_id=args.classroom_id)
    student_ids = [sid.strip() for sid in args.students.split(",") if sid.strip()]
    comp = compare_answers(
        composition=composition,
        assessment_id=args.assessment,
        question_id=args.question,
        student_ids=student_ids,
    )
    if args.format == "json":
        print(_json.dumps({
            "assessment_id": comp.assessment_id,
            "question_id": comp.question_id,
            "score_spread": comp.score_spread,
            "rows": [
                {
                    "student_id": r.student_id,
                    "answer": r.answer,
                    "final_score": r.final_score,
                    "question_type": r.question_type,
                    "reviewed_by": r.reviewed_by,
                    "trace_timestamp": r.trace_timestamp,
                }
                for r in comp.rows
            ],
        }, indent=2))
    else:
        print(render_markdown(comp))
    return 0


def _load_course_for_checkpoint_op(course_id: str) -> tuple[Any, dict] | None:
    """Load a course's workflow + data for a checkpoint mutation."""
    from .operational_store import load_course

    return load_course(course_id)


def _save_course_manifest_update(course_id: str, data: dict, manifest: dict) -> None:
    """Persist a manifest mutation back onto the course artifact."""
    from .operational_store import _reg

    updated = dict(data)
    updated["manifest"] = manifest
    _reg().register(kind="course", name=course_id, data=updated)


def _cmd_wrap_template(args: argparse.Namespace) -> int:
    from pathlib import Path as _Path

    import yaml as _yaml

    from .conclusion import propose_template_update

    result = propose_template_update(classroom_id=args.classroom_id)

    if "error" in result:
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            from . import ui
            ui.emit_error(result["error"])
        return 1

    from . import ui
    if args.out:
        out_path = _Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            _yaml.safe_dump(result["proposed_manifest"], default_flow_style=False)
        )
        payload = {
            "classroom_id": args.classroom_id,
            "written_to": str(out_path),
            "rationale": result["rationale"],
        }
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0
        ui.emit_success(f"Proposed manifest written to {out_path}")
        if result["rationale"]:
            ui.out().print()
            ui.out().print("[bold]Rationale[/]")
            for r in result["rationale"]:
                sig = r.get("signal", "note")
                tag = r.get("assessment_id", sig)
                ui.out().print(f"  [cyan]{tag}[/] {r.get('suggestion', '')}")
        return 0

    # No --out: print the proposal inline
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(_yaml.safe_dump(result["proposed_manifest"], default_flow_style=False))
    if result["rationale"]:
        ui.out().print()
        ui.out().print("[bold]Rationale[/]")
        for r in result["rationale"]:
            sig = r.get("signal", "note")
            tag = r.get("assessment_id", sig)
            ui.out().print(f"  [cyan]{tag}[/] {r.get('suggestion', '')}")
    return 0


def _cmd_wrap_grades(args: argparse.Namespace) -> int:
    from .conclusion import finalize_grades

    result = finalize_grades(
        classroom_id=args.classroom_id,
        push=args.push,
        canvas_course_id=args.canvas_course_id or None,
        canvas_assignment_id=args.canvas_assignment_id or None,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        if "error" in result:
            return 1
        if args.push and result.get("failures"):
            return 1
        return 0

    from . import ui
    if "error" in result:
        ui.emit_error(result["error"])
        return 1

    ui.emit_success(f"Final grades computed for \"{args.classroom_id}\".")
    ui.emit_info(f"Formula: {result['formula']}")
    rows = [
        [
            s["student_id"],
            f"{s['final_grade']:.3f}",
            str(s["assessments_graded"]),
        ]
        for s in result["students"]
    ]
    ui.emit_table(
        title=None,
        columns=["Student", "Final grade", "Assessments"],
        rows=rows,
    )
    if args.push:
        total = len(result["students"])
        failures = result.get("failures") or []
        n_ok = total - len(failures)
        if failures:
            ui.emit_warn(f"Pushed to Canvas: {n_ok}/{total} succeeded.")
            for f in failures:
                ui.out().print(f"  [red]✗[/] {f['student_id']}: {f['error']}")
            return 1
        ui.emit_success(f"Pushed to Canvas: {n_ok}/{total} succeeded.")
    return 0


def _cmd_wrap_harvest(args: argparse.Namespace) -> int:
    from .conclusion import harvest_classroom

    result = harvest_classroom(
        classroom_id=args.classroom_id, out_path=args.out,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("harvested") else 1

    from . import ui
    if result.get("harvested"):
        ui.emit_success(f"Harvested classroom \"{args.classroom_id}\".")
        ui.emit_kv("Details", {
            "path": result["path"],
            "harvested at": _friendly_expiry(result["harvested_at"]),
        })
        return 0
    ui.emit_error(result.get("error", "harvest failed"))
    return 1


def _cmd_wrap_analytics(args: argparse.Namespace) -> int:
    from .conclusion import format_summary_markdown, summarize_classroom

    summary = summarize_classroom(args.classroom_id)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(format_summary_markdown(summary))
    return 0 if "error" not in summary else 1


def _cmd_enroll(args: argparse.Namespace) -> int:
    from . import ui
    from .enroll_runner import run_enrollment

    result = run_enrollment(
        classroom_id=args.classroom_id,
        instructor=args.instructor,
        fake=args.fake,
        ttl_days=args.ttl_days,
        canvas_course_id=(args.canvas_course_id or None),
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("enrolled") else 1

    if not result.get("enrolled"):
        ui.emit_error(result.get("error", "enrollment failed"))
        return 1

    n = result["student_count"]
    ui.emit_success(
        f"Enrolled {n} student{'' if n == 1 else 's'} in \"{args.classroom_id}\"."
    )
    rows = []
    for t in result.get("tokens", []):
        name = t.get("name") or t.get("student_id")
        rows.append([name, t["expires_at"][:10]])
    if rows:
        ui.emit_table(
            title=None,
            columns=["Student", "Token expires"],
            rows=rows,
        )
    rail_count = result.get("rail_count", 0)
    if rail_count:
        ui.emit_info(
            f"{rail_count} onboarding rail{'' if rail_count == 1 else 's'} "
            "queued per student."
        )
    return 0


def _humanize_join_error(raw_error: str) -> str:
    """Translate common ceremony error reasons into student-friendly phrasing.

    The lower layers produce accurate, technical messages (signature
    verification failed, token not recognized, etc). Students shouldn't
    have to parse those — we recognize common patterns and rewrite into
    a plain-language reason with a suggested next action. Unrecognized
    messages pass through with a soft prefix.
    """
    if not raw_error:
        return "Something went wrong — please ask your instructor to resend the invite."

    lower = raw_error.lower()
    # Token-level problems that an instructor can fix by reissuing.
    if "not recognized" in lower or "unknown" in lower and "invite" in lower:
        return (
            "This invite isn't recognized by the classroom server. "
            "Ask your instructor to send you a fresh invite."
        )
    if "expired" in lower:
        return (
            "This invite has expired. Ask your instructor to send you "
            "a new one."
        )
    if "already consumed" in lower or "reuse" in lower:
        return (
            "This invite has already been used. Ask your instructor to "
            "send you a fresh one if you need to join again."
        )
    # Signature / tampering — usually a corrupted or copy-paste-damaged invite.
    if "signature" in lower or "tampered" in lower or "malformed" in lower:
        return (
            "This invite looks damaged (copy-paste issue?). "
            "Try copying the full invite from your instructor's message again."
        )
    # Network-ish transports.
    if "transport error" in lower or "http " in lower:
        return (
            "Couldn't reach the classroom server. Check your internet "
            "connection and try again in a minute."
        )
    # Unknown — still show the raw message but wrap it gently.
    return f"Couldn't join — the classroom server said: {raw_error}"


def _infer_default_owner() -> str:
    """Best-effort owner string for auto-generated node identity.

    Priority: git user.email → ``$USER@<hostname>`` → "student". Never
    fails — identity generation shouldn't block on owner-resolution
    edge cases.
    """
    import socket
    import subprocess

    try:
        from axiom.infra.git import safe_git_env
        git_email = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True,
            text=True,
            timeout=2,
            env=safe_git_env(),
        )
        if git_email.returncode == 0:
            email = git_email.stdout.strip()
            if email:
                return email
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    user = os.environ.get("USER") or os.environ.get("USERNAME")
    if user:
        try:
            host = socket.gethostname() or "localhost"
        except OSError:
            host = "localhost"
        return f"{user}@{host}"

    return "student"


def _cmd_join(args: argparse.Namespace) -> int:
    """Student-side classroom-join command.

    Two modes on the same subcommand:
      - **Dry-run** (invite has no embedded coordinator_url AND no
        ``--coordinator`` flag): decode + validate invite TTL, print
        what cohort + coordinator would be joined.
      - **Full ceremony** (coordinator URL available from either
        source): sign + POST the request, verify + persist the
        returned manifest.

    Identity auto-init: if `axi federation init` has never been run on
    this machine, the command generates an identity on the fly with
    inferred owner (git email / $USER@hostname) rather than erroring
    out. See `feedback_proactive_ux_minimize_cognitive_load`.
    """
    from .invite_token import (
        InvalidInviteError,
        decode_invite,
        validate_invite_token,
    )

    try:
        invite = decode_invite(args.invite)
    except InvalidInviteError as exc:
        if args.json:
            print(json.dumps({"accepted": False, "error": str(exc)}, indent=2))
        else:
            from . import ui
            ui.emit_invite_damaged()
        return 1

    # Coordinator URL resolution — flag > invite-embedded. If neither,
    # stay in preview mode (user sees a preview + next step).
    coordinator_url = args.coordinator or invite.coordinator_url

    # ---- Preview path (no classroom-server URL available anywhere) ------
    if not coordinator_url:
        result = validate_invite_token(invite)
        payload = {
            "accepted": result.valid,
            "classroom_id": invite.classroom_id,
            "coordinator_id": invite.coordinator_id,
            "expires": invite.expires,
            "dry_run": True,
        }
        if not result.valid:
            payload["error"] = result.reason

        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            if result.valid:
                print(f"Invite for classroom \"{invite.classroom_id}\" looks good.")
                print(f"  Expires: {invite.expires}")
                print()
                print("This invite doesn't include the classroom server URL.")
                print("Ask your instructor for it, then run:")
                print(f"  axi classroom join {args.invite[:20]}... --coordinator <URL>")
            else:
                print(_humanize_join_error(result.reason or ""), file=sys.stderr)
        return 0 if result.valid else 1

    # ---- Full ceremony path ---------------------------------------------
    from axiom.vega.federation.identity import generate_identity, load_identity

    from . import ui
    from .classroom_client import (
        ClassroomJoinClient,
        JoinClientError,
    )
    from .classroom_join_http import UrllibTransport
    from .student_membership import MembershipStore

    identity = load_identity()
    if identity is None:
        owner = _infer_default_owner()
        if not args.json:
            ui.emit_identity_autoinit(owner)
        identity = generate_identity(owner=owner)

    student_id = args.student_id or identity.owner
    client = ClassroomJoinClient(
        student_identity=identity,
        transport=UrllibTransport(),
        store=MembershipStore(base_dir=Path.home() / ".axi"),
    )

    spinner_ctx = (
        ui.joining_spinner(invite.classroom_id)
        if not args.json
        else _null_spinner()
    )
    try:
        with spinner_ctx:
            result = client.join(
                encoded_invite=args.invite,
                student_id=student_id,
                coordinator_url=coordinator_url,
            )
    except JoinClientError as exc:
        if args.json:
            print(json.dumps({"accepted": False, "error": str(exc)}, indent=2))
        else:
            print(_humanize_join_error(str(exc)), file=sys.stderr)
        return 1

    payload = {
        "accepted": result.accepted,
        "classroom_id": invite.classroom_id,
        "coordinator_url": coordinator_url,
    }
    if result.accepted:
        payload["student_id"] = result.membership.student_id
        payload["coordinator_node"] = result.membership.coordinator_node
        payload["joined_at"] = result.membership.manifest.joined_at
    else:
        payload["error"] = result.error

    # ---- Materials sync + local indexing (Phase 5) ----------------------
    # After a successful join, pull the signed materials manifest, verify
    # every file hash, and build a local vector+graph index. Graceful
    # skip if the coordinator doesn't serve materials (older versions)
    # or if the student machine can't embed (the index still supports
    # keyword search via FTS5).
    if result.accepted:
        _sync_and_index_materials(
            coordinator_base_url=_strip_join_suffix(coordinator_url),
            classroom_id=invite.classroom_id,
            coordinator_public_key=result.membership.coordinator_public_key,
            json_mode=bool(args.json),
        )

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        if result.accepted:
            m = result.membership
            ui.emit_join_success(m.classroom_id, m.student_id)
        else:
            print(_humanize_join_error(result.error or ""), file=sys.stderr)

    return 0 if result.accepted else 1


def _strip_join_suffix(url: str) -> str:
    """``http://host:port/classroom/join`` → ``http://host:port``."""
    suffix = "/classroom/join"
    if url.endswith(suffix):
        return url[: -len(suffix)]
    return url


def _ensure_join_suffix(url: str) -> str:
    """``http://host:port`` → ``http://host:port/classroom/join``.

    The student-side join client POSTs the literal ``coordinator_url``
    from the invite, so the URL must include the ``/classroom/join``
    path. Instructors who pass a bare base URL to ``invite
    --coordinator-url`` would otherwise mint an invite that fails with
    a misleading "couldn't reach the classroom server" on the student
    side. Normalising here keeps the invariant in one place.
    """
    suffix = "/classroom/join"
    trimmed = url.rstrip("/")
    if trimmed.endswith(suffix):
        return trimmed
    return trimmed + suffix


def _require_active(classroom_id: str) -> bool:
    """Reject the command if the classroom is archived.

    Archive is the terminal lifecycle state (`archive.py`); all
    state-mutating instructor commands must refuse to run on an
    archived classroom or the audit record diverges from reality.
    Returns True when it's safe to proceed.
    """
    from . import ui
    from .archive import is_archived

    if is_archived(classroom_id):
        ui.emit_error(
            f"Classroom {classroom_id!r} is archived. Clone into a new "
            f"classroom to start a fresh cohort: "
            f"axi classroom prep from-demo --from {classroom_id}"
        )
        return False
    return True


def _sync_and_index_materials(
    *,
    coordinator_base_url: str,
    classroom_id: str,
    coordinator_public_key: str,
    json_mode: bool,
) -> None:
    """Pull classroom materials from the coordinator, build a local index.

    Non-fatal: any failure (network, server has no materials, embedder
    absent) is narrated and swallowed. The student still successfully
    joined; materials are an enhancement, not a gate.
    """
    from . import ui
    from .classroom_join_http import UrllibTransport
    from .classroom_local_index import ClassroomLocalIndex
    from .materials_sync import (
        MaterialsSyncClient,
        MaterialsTamperError,
        StudentMaterialsStore,
    )

    classroom_dir = Path.home() / ".axi" / "classrooms" / classroom_id
    # Remember the coordinator base URL on disk — the `ask` command
    # uses it later to push interaction signals back so CHALKE can
    # surface "hot topics" and "quiet students" in the instructor brief.
    classroom_dir.mkdir(parents=True, exist_ok=True)
    (classroom_dir / "coordinator_url.txt").write_text(coordinator_base_url)

    student_store = StudentMaterialsStore(classroom_dir)
    client = MaterialsSyncClient(
        transport=UrllibTransport(),
        store=student_store,
        coordinator_public_key=coordinator_public_key,
    )
    try:
        sync_result = client.sync(base_url=coordinator_base_url)
    except MaterialsTamperError as exc:
        if not json_mode:
            ui.emit_error(f"Course materials verification failed: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 — sync is best-effort
        if not json_mode:
            ui.emit_info(
                f"[dim](Couldn't download course materials: {exc}. "
                "Joined anyway.)[/]"
            )
        return

    if not sync_result.accepted:
        # Older coordinator without materials endpoints, or 404 on files.
        # Joining is still a success — stay quiet unless the sync actually
        # got started.
        if sync_result.downloaded > 0 and not json_mode:
            ui.emit_info(
                f"[dim](Partial materials download — "
                f"{sync_result.downloaded} file(s) before error.)[/]"
            )
        return

    total = sync_result.downloaded + sync_result.cached
    if total == 0:
        return  # nothing to say

    # Index every downloaded file into the local classroom index.
    index = ClassroomLocalIndex(base_dir=classroom_dir)
    index.open()
    indexed_count = 0
    try:
        for entry in student_store.list_entries():
            try:
                content = student_store.get_path(entry["file_id"]).read_bytes()
                text = content.decode("utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            index.ingest(
                file_id=entry["file_id"],
                title=entry["title"],
                content=text,
                embed=None,  # FTS-only for now; Phase 4b wires real embedder
            )
            indexed_count += 1
    finally:
        index.close()

    if not json_mode:
        references = _count_index_references(classroom_dir)
        file_word = "file" if total == 1 else "files"
        ref_word = "reference" if references == 1 else "references"
        ui.emit_info(
            f"Downloaded {total} course {file_word} "
            f"({references} {ref_word} loaded)."
        )


def _count_index_references(classroom_dir: Path) -> int:
    """How many chunks ended up in the local index — student-friendly count."""
    from .classroom_local_index import ClassroomLocalIndex

    index = ClassroomLocalIndex(base_dir=classroom_dir)
    index.open()
    try:
        return index.chunk_count()
    finally:
        index.close()


@contextmanager
def _null_spinner():
    """No-op context for --json callers that need a uniform spinner interface."""
    yield


def _cmd_invite(args: argparse.Namespace) -> int:
    """Instructor-side: mint an invite for a student to copy-paste.

    Auto-initializes identity + cohort on first run. Records the
    coordinator URL once so future invites for this class don't need
    `--coordinator-url`. Prints a framed email-ready snippet that the
    instructor can forward verbatim.
    """
    from axiom.vega.federation.identity import generate_identity, load_identity

    from .classroom_federation import create_cohort
    from .coordinator_cohort_store import CohortNotFoundError, FileCohortStore
    from .coordinator_invite_registry import FileInviteRegistry
    from .invite_token import create_invite_token, encode_invite

    coordinator_dir = Path.home() / ".axi" / "coordinator"
    cohort_store = FileCohortStore(coordinator_dir)
    registry = FileInviteRegistry(coordinator_dir / "invites.json")

    # Resolve coordinator URL: flag > stored for this class > error.
    stored_url: str | None = None
    if cohort_store.exists(args.classroom_id):
        try:
            stored_url = cohort_store.get_coordinator_url(args.classroom_id)
        except (CohortNotFoundError, ValueError):
            stored_url = None

    coordinator_url = args.coordinator_url or stored_url
    if not coordinator_url:
        from . import ui
        ui.emit_need_coordinator_url(args.classroom_id)
        return 1
    coordinator_url = _ensure_join_suffix(coordinator_url)

    # Auto-init identity (same proactive-UX pattern as student join).
    identity = load_identity()
    if identity is None:
        owner = _infer_default_owner()
        if not args.json:
            from . import ui
            ui.emit_identity_autoinit(owner)
        identity = generate_identity(owner=owner)

    # Auto-create cohort if absent.
    if cohort_store.exists(args.classroom_id):
        cohort = cohort_store.load(args.classroom_id)
    else:
        cohort = create_cohort(args.classroom_id, identity.node_id)

    count = max(1, getattr(args, "count", 1) or 1)
    invites: list = []
    for _ in range(count):
        inv = create_invite_token(
            classroom_id=args.classroom_id,
            coordinator_id=identity.node_id,
            ttl_hours=args.ttl_hours,
            coordinator_url=coordinator_url,
        )
        registry.register(inv)
        invites.append(inv)
    cohort_store.save(cohort, coordinator_url=coordinator_url)

    encoded_list = [encode_invite(inv) for inv in invites]

    if args.json:
        if count == 1:
            # Backward-compat: single-invite JSON shape stays the same.
            print(json.dumps({
                "classroom_id": args.classroom_id,
                "invite": encoded_list[0],
                "expires": invites[0].expires,
            }, indent=2))
        else:
            print(json.dumps({
                "classroom_id": args.classroom_id,
                "invites": [
                    {"invite": e, "expires": inv.expires}
                    for e, inv in zip(encoded_list, invites)
                ],
            }, indent=2))
        return 0

    from . import ui
    if count == 1:
        ui.emit_invite_ready(
            classroom_id=args.classroom_id,
            encoded=encoded_list[0],
            expiry_friendly=_friendly_expiry(invites[0].expires),
        )
    else:
        ui.out().print()
        ui.out().print(
            f"Minted [bold]{count}[/] single-use invites for class "
            f"[bold]\"{args.classroom_id}\"[/]. "
            f"Each is independent — pass one per student. "
            f"Expires: {_friendly_expiry(invites[0].expires)}."
        )
        ui.out().print()
        for i, encoded in enumerate(encoded_list, start=1):
            ui.out().print(f"[dim]{i:>3}.[/]  axi classroom join {encoded}")
        ui.out().print()
    return 0


def _friendly_expiry(iso_ts: str) -> str:
    """Turn '2026-04-29T17:33:32+00:00' into 'Wed, Apr 29 at 5:33 PM UTC'.

    Best-effort; on any parse failure, returns the raw string so the
    instructor still sees *something* useful.
    """
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts
    # %-I / %-M aren't available on every platform; use %I and strip the
    # leading zero by hand.
    formatted = dt.strftime("%a, %b %d at %I:%M %p %Z").strip()
    formatted = formatted.replace(" 0", " ", 1)  # "05:33" → "5:33"
    if not dt.tzinfo or not dt.strftime("%Z"):
        formatted += " UTC"
    return formatted


def _cmd_serve(args: argparse.Namespace) -> int:
    """Instructor-side: run the coordinator HTTP server (FastAPI + uvicorn).

    Assumes the instructor ran ``axi classroom invite <classroom_id>``
    at least once to bootstrap their identity + cohort + coordinator
    URL. If not, prints the one-line command to run first.
    """
    import time

    from axiom.extensions.builtins.http import ThreadedServer
    from axiom.memory.adapters import interaction_writer
    from axiom.vega.federation.identity import load_identity

    from . import ui
    from .broadcast_quizzes import QuizStore
    from .classroom_api import create_classroom_app
    from .classroom_interaction import ClassroomInteractionStore
    from .classroom_materials import ClassroomMaterialsStore
    from .classroom_threads import ThreadStore
    from .composition_boot import build_classroom_composition
    from .coordinator_cohort_store import CohortNotFoundError, FileCohortStore
    from .coordinator_invite_registry import FileInviteRegistry
    from .student_briefs import BriefStore

    coord_dir = Path.home() / ".axi" / "coordinator"
    cohort_store = FileCohortStore(coord_dir)
    registry = FileInviteRegistry(coord_dir / "invites.json")
    classroom_coord_dir = coord_dir / "classrooms" / args.classroom_id
    materials_store = ClassroomMaterialsStore(classroom_coord_dir)

    # ADR-033 Stage 1: every interaction also lands in the canonical
    # L1 memory layer via CompositionService. The bespoke JSONL stays
    # authoritative until Stage 4; the L1 mirror enables the
    # RecentActivityProjection (Layer 3) that the student-side ask
    # path consumes for episodic context.
    composition = build_classroom_composition(args.classroom_id)
    interaction_store = ClassroomInteractionStore(
        classroom_coord_dir,
        memory_writer=interaction_writer(composition),
        scope_id=args.classroom_id,
    )
    brief_store = BriefStore(classroom_coord_dir)
    thread_store = ThreadStore(classroom_coord_dir)
    quiz_store = QuizStore(classroom_coord_dir)

    if not cohort_store.exists(args.classroom_id):
        ui.emit_serve_needs_bootstrap(args.classroom_id)
        return 1

    identity = load_identity()
    if identity is None:
        # Shouldn't happen in normal flow (invite auto-initialized it),
        # but recover anyway rather than blow up.
        from axiom.vega.federation.identity import generate_identity
        owner = _infer_default_owner()
        ui.emit_identity_autoinit(owner)
        identity = generate_identity(owner=owner)

    try:
        cohort_store.load(args.classroom_id)
    except CohortNotFoundError:
        ui.emit_serve_needs_bootstrap(args.classroom_id)
        return 1

    def _on_student_joined(student_id: str) -> None:
        ui.emit_student_joined(student_id)

    app = create_classroom_app(
        coordinator_identity=identity,
        classroom_id=args.classroom_id,
        cohort_store=cohort_store,
        invite_registry=registry,
        on_student_joined=_on_student_joined,
        materials_store=materials_store,
        interaction_store=interaction_store,
        brief_store=brief_store,
        thread_store=thread_store,
        quiz_store=quiz_store,
        artifact_registry=composition.artifact_registry,
    )

    # ThreadedServer gives us the bound port up-front (important when
    # port=0) so the startup banner shows the real URL students will
    # hit. We then block on the server thread until Ctrl-C.
    server = ThreadedServer(app, host=args.host, port=args.port)
    try:
        server.start()
    except Exception as exc:
        ui.emit_error(f"Couldn't start server: {exc}")
        return 1

    url = f"http://{args.host}:{server.bound_port}/classroom/join"
    public_url = cohort_store.get_coordinator_url(args.classroom_id)

    ui.emit_serve_banner(
        classroom_id=args.classroom_id,
        local_url=url,
        public_url=public_url,
    )

    try:
        # Block until Ctrl-C. The server thread stays alive until
        # server.shutdown() runs in the finally branch.
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        ui.emit_serve_stopping()
    finally:
        server.shutdown()
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    """Student-side Q&A — grounded answer + citations from the local index.

    Wired through the generic :class:`axiom.memory.ask.AskPipeline`:

      1. Resolve the effective learning mode (instructor forced >
         student preference bounded by classroom policy > default).
      2. Build a :class:`ClassroomRetriever` over the per-classroom
         local index and a :class:`ClassroomAskHooks` that contributes
         mode-specific system-prompt overlays + closed-book short-
         circuiting.
      3. The pipeline composes the prompt via PromptComposer (seven
         layers, including local L1 episodic memory), invokes the LLM
         via the Gateway, logs the interaction to L1, and returns a
         typed AskResult.
      4. Render the result for the mode (terminal or JSON).

    The pipeline path replaces an earlier raw-string-concat prompt
    builder so concept-aware retrieval, RecentActivityProjection
    contributions, and L1 episodic logging happen automatically rather
    than reinvented per call site.
    """
    from axiom.memory.ask import AskPipeline, AskRequest
    from axiom.memory.bootstrap import build_memory_stack

    from . import ui
    from .ask_hooks import ClassroomAskHooks, ClassroomRetriever
    from .learning_modes import effective_mode, get_mode
    from .student_mode_state import (
        load_preference,
        resolve_policy,
        save_preference,
    )

    class_dir = Path.home() / ".axi" / "classrooms" / args.classroom_id
    if not class_dir.is_dir():
        if args.json:
            print(json.dumps({
                "error": f"not a member of {args.classroom_id!r}",
            }))
        else:
            ui.emit_error(
                f"You're not in class \"{args.classroom_id}\" yet. "
                "Ask your instructor for the invite, then:\n"
                f"  axi classroom join <invite-from-your-instructor>"
            )
        return 1

    # --- Mode resolution ---
    coord_url_file = class_dir / "coordinator_url.txt"
    coord_base_url = (
        coord_url_file.read_text().strip()
        if coord_url_file.is_file() else None
    )
    policy = resolve_policy(
        classroom_dir=class_dir,
        coordinator_base_url=coord_base_url,
    )
    preference = args.mode or load_preference(class_dir)
    mode_name = effective_mode(policy=policy, student_preference=preference)
    get_mode(mode_name)  # validates mode_name resolves; result not used here

    # Remember the student's EXPLICIT preference (--mode) for next time.
    # Don't stash the effective_mode result; if instructor lifts a
    # forced-mode restriction, the student's own preference should
    # resurface.
    if args.mode:
        save_preference(class_dir, args.mode)

    # If the instructor forced a mode different from what the student
    # asked for, surface that so they're not confused.
    if args.mode and mode_name != args.mode and not args.json:
        ui.emit_info(
            f"Your instructor has set the class to [bold]{mode_name}[/] "
            f"mode right now (overriding your '{args.mode}' pick)."
        )

    # --- Resolve student principal id for L1 logging. Best-effort:
    # fall back to a deterministic local label if membership store is
    # unavailable (e.g., a student running ask before a full join). ---
    from .student_membership import (
        MembershipNotFoundError,
        MembershipStore,
    )
    try:
        stored = MembershipStore(base_dir=Path.home() / ".axi").load(
            args.classroom_id,
        )
        principal_id = stored.student_id
    except MembershipNotFoundError:
        principal_id = f"local:{args.classroom_id}"

    # --- Build memory stack + pipeline ---
    scope_id = f"classroom:{args.classroom_id}"
    memory_stack = build_memory_stack(scope_id=scope_id)

    retriever = ClassroomRetriever(classroom_dir=class_dir)
    hooks = ClassroomAskHooks(classroom_id=args.classroom_id)

    # Gateway-backed LLM adapter. Constructed lazily inside the
    # closure so ``--cite-only`` runs (which short-circuit before the
    # pipeline calls the LLM) never instantiate a Gateway. Exceptions
    # fall through to citations-only — the student still gets useful
    # output even when the provider explodes.
    def _llm(*, system_blocks, user_message, task):
        from axiom.infra.gateway import Gateway

        gateway = Gateway()
        # The Gateway exposes a flat-string ``system`` parameter; the
        # pipeline composes content blocks (Anthropic format) so we
        # join them for providers that don't speak content-blocks.
        system_text = "\n\n".join(
            b.get("text", "") for b in system_blocks
        ).strip()
        try:
            resp = gateway.complete(
                prompt=user_message,
                system=system_text,
                task="classroom_qna",
            )
        except Exception:
            return None
        if not getattr(resp, "success", False):
            return None
        return getattr(resp, "text", None) or None

    pipeline = AskPipeline(
        memory_stack=memory_stack,
        retriever=retriever,
        llm=_llm,
        hooks=hooks,
    )

    request = AskRequest(
        question=args.question,
        principal_id=principal_id,
        scope_id=scope_id,
        mode=mode_name,
        cite_only=bool(args.cite_only),
        k_citations=int(args.k),
    )

    try:
        result = pipeline.ask(request)
    finally:
        retriever.close()

    citations = result.citations
    answer = result.answer

    if args.json:
        print(json.dumps({
            "question": args.question,
            "classroom_id": args.classroom_id,
            "mode": mode_name,
            "answer": answer,
            "citations": [
                {
                    "title": c.title,
                    "file_id": c.source_id,
                    "text": c.text,
                    "score": c.score,
                }
                for c in citations
            ],
        }, indent=2))
        _push_interaction_best_effort(
            classroom_id=args.classroom_id,
            classroom_dir=class_dir,
            question=args.question,
            had_answer=bool(answer or citations),
            citations_count=len(citations),
            mode=mode_name,
        )
        return 0

    ui.out().print()
    ui.out().print(f"[dim]Q:[/] [bold]{args.question}[/]")
    ui.out().print()

    if not citations:
        ui.emit_info(
            "No matching passages in your class materials. "
            "Try rephrasing, or ask in class."
        )
        _push_interaction_best_effort(
            classroom_id=args.classroom_id,
            classroom_dir=class_dir,
            question=args.question,
            had_answer=False,
            citations_count=0,
            mode=mode_name,
        )
        return 0

    if answer:
        ui.out().print(answer)
        ui.out().print()

    n = len(citations)
    citation_word = "citation" if n == 1 else "citations"
    header = "Sources" if answer else f"Top {n} {citation_word} from your class"
    ui.out().print(f"[dim]{header}:[/]")
    for c in citations:
        ui.out().print()
        ui.out().print(f"  [bold cyan]{c.title}[/]")
        for line in c.text.splitlines():
            if line.strip():
                ui.out().print(f"  [dim]│[/] {line.strip()}")

    _push_interaction_best_effort(
        classroom_id=args.classroom_id,
        classroom_dir=class_dir,
        question=args.question,
        had_answer=bool(answer or citations),
        citations_count=len(citations),
        mode=mode_name,
    )
    return 0


def _push_interaction_best_effort(
    *,
    classroom_id: str,
    classroom_dir: Path,
    question: str,
    had_answer: bool,
    citations_count: int,
    mode: str | None = None,
) -> None:
    """POST one interaction record back to the coordinator.

    Best-effort — any failure (network down, coordinator old, URL
    sidecar missing) is swallowed so the student's `ask` command
    still returns cleanly. Interaction reporting is a secondary
    concern; correctness of the student-visible answer is primary.
    """
    import urllib.error
    import urllib.request

    # Need the student's membership to name themselves in the log.
    from .student_membership import MembershipNotFoundError, MembershipStore

    url_sidecar = classroom_dir / "coordinator_url.txt"
    if not url_sidecar.is_file():
        return
    base_url = url_sidecar.read_text().strip()
    if not base_url:
        return

    try:
        store = MembershipStore(base_dir=Path.home() / ".axi")
        stored = store.load(classroom_id)
        student_id = stored.student_id
    except MembershipNotFoundError:
        return

    payload = {
        "student_id": student_id,
        "question": question,
        "had_answer": had_answer,
        "citations_count": citations_count,
    }
    if mode:
        payload["mode"] = mode
    body = json.dumps(payload).encode("utf-8")
    target = base_url.rstrip("/") + "/classroom/interaction"
    req = urllib.request.Request(
        target,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0):
            pass
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        pass


def _cmd_evals(args: argparse.Namespace) -> int:
    """Instructor-side evals runner.

    Loads a JSONL question bank, retrieves + answers each question
    against the classroom's local index, and reports pass/fail scoring.
    Meant as the inside-out verifier before real students arrive:
    "does my class actually answer what I expect it to?"
    """
    from . import ui
    from .classroom_evals import compare, load_bank, run_bank, run_baseline
    from .classroom_local_index import ClassroomLocalIndex
    from .classroom_qna import Citation

    # Resolve --bank OR --bank-preset to a concrete path.
    if args.bank_preset:
        banks_dir = Path(__file__).parent / "banks"
        if args.bank_preset == "list":
            available = sorted(p.stem for p in banks_dir.glob("*.jsonl"))
            if not available:
                ui.emit_info("No bank presets shipped yet.")
                return 0
            ui.emit_info("Bank presets available:")
            for name in available:
                ui.out().print(f"  [cyan]{name}[/]")
            return 0
        # Accept either 'ne101-core' or 'ne101_core' — the on-disk
        # filenames use underscores but hyphens read better in CLI.
        candidate = banks_dir / f"{args.bank_preset.replace('-', '_')}.jsonl"
        if not candidate.is_file():
            available = sorted(p.stem for p in banks_dir.glob("*.jsonl"))
            ui.emit_error(
                f"No bank preset named '{args.bank_preset}'. "
                f"Available: {', '.join(available) if available else '(none)'}"
            )
            return 1
        bank_path = candidate
    else:
        bank_path = Path(args.bank)
        if not bank_path.is_file():
            ui.emit_error(f"Question bank not found: {bank_path}")
            return 1
    try:
        bank = load_bank(bank_path)
    except ValueError as exc:
        ui.emit_error(f"Question bank is invalid: {exc}")
        return 1

    # Two index sources, in order of preference:
    # 1. Student-side already-built index (post-join)
    # 2. Instructor-side fallback: build a transient index from the
    #    coordinator materials dir — lets an instructor run evals
    #    before ever joining their own class.
    student_dir = Path.home() / ".axi" / "classrooms" / args.classroom_id
    coord_materials_dir = (
        Path.home() / ".axi" / "coordinator" / "classrooms" / args.classroom_id
    )
    student_index_db = student_dir / "index.db"

    transient_dir: Path | None = None
    if student_index_db.is_file():
        class_dir = student_dir
    elif (coord_materials_dir / "materials").is_dir():
        # Instructor self-eval — build a transient index from their
        # coordinator materials. Narrate (stderr) so the instructor
        # knows they're not hitting a cached student view.
        import tempfile

        from .classroom_materials import ClassroomMaterialsStore

        if not args.json:
            ui.emit_info(
                f"Using your coordinator materials for \"{args.classroom_id}\" "
                "(no student index on this machine yet)."
            )
        transient_dir = Path(tempfile.mkdtemp(prefix="axi-evals-"))
        class_dir = transient_dir
        # Seed the transient index.
        seed_index = ClassroomLocalIndex(base_dir=transient_dir)
        seed_index.open()
        try:
            src = ClassroomMaterialsStore(coord_materials_dir)
            for entry in src.list_entries():
                content = src.get_path(entry.file_id).read_bytes()
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                seed_index.ingest(
                    file_id=entry.file_id,
                    title=entry.title,
                    content=text,
                    embed=None,
                )
        finally:
            seed_index.close()
    else:
        ui.emit_error(
            f"No materials for class \"{args.classroom_id}\" on this machine.\n"
            "  Instructor: upload via  [cyan]axi classroom prep corpus[/] first\n"
            "  Student:   join via    [cyan]axi classroom join <invite>[/] first"
        )
        return 1

    index = ClassroomLocalIndex(base_dir=class_dir)
    index.open()

    def _retrieve(question: str, k: int) -> list[Citation]:
        hits = index.search(question, k=k)
        return [
            Citation(title=h.title, text=h.text, file_id=h.file_id)
            for h in hits
        ]

    def _llm(prompt: str, *, system: str = "") -> str | None:
        if args.cite_only:
            return None
        from axiom.infra.gateway import Gateway

        gw = Gateway()
        resp = gw.complete(prompt=prompt, system=system, task="classroom_evals")
        if not getattr(resp, "success", False):
            return None
        return getattr(resp, "text", None) or None

    try:
        report = run_bank(
            bank=bank,
            retrieve=_retrieve,
            llm=_llm,
            k=args.k,
        )
        baseline_report = None
        comparison = None
        if args.baseline and not args.cite_only:
            baseline_report = run_baseline(bank=bank, llm=_llm)
            comparison = compare(
                axiom_report=report, baseline_report=baseline_report,
            )
    finally:
        index.close()

    if args.json:
        payload = {
            "classroom_id": args.classroom_id,
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "pass_rate": report.pass_rate,
            "results": [
                {
                    "question": r.question.question,
                    "expected_keywords": r.question.expected_keywords,
                    "answer": r.answer,
                    "passed": r.score.passed,
                    "hit_keywords": r.score.hit_keywords,
                    "missed_keywords": r.score.missed_keywords,
                    "citations": [
                        {"title": c.title, "file_id": c.file_id}
                        for c in r.citations
                    ],
                }
                for r in report.results
            ],
        }
        if comparison is not None:
            payload["baseline"] = {
                "passed": baseline_report.passed,
                "failed": baseline_report.failed,
                "pass_rate": baseline_report.pass_rate,
            }
            payload["lift"] = comparison.lift
            payload["axiom_only_wins"] = comparison.axiom_only_wins
            payload["baseline_only_wins"] = comparison.baseline_only_wins
        if args.min_pass_rate is not None:
            payload["min_pass_rate"] = args.min_pass_rate
            payload["gate_passed"] = report.pass_rate >= args.min_pass_rate
        print(json.dumps(payload, indent=2))
        _cleanup_transient(transient_dir)
        return _evals_exit_code(report, args.min_pass_rate)

    # Rich summary
    ui.out().print()
    ui.out().print(
        f"[bold]Evals for \"{args.classroom_id}\"[/]  "
        f"[dim]({bank_path.name}, {report.total} question"
        f"{'' if report.total == 1 else 's'})[/]"
    )
    ui.out().print()

    rows = []
    for r in report.results:
        glyph = "[green]✓[/]" if r.score.passed else "[red]✗[/]"
        missed = ", ".join(r.score.missed_keywords) if r.score.missed_keywords else "—"
        rows.append([
            glyph,
            (r.question.question[:48] + "…")
            if len(r.question.question) > 49 else r.question.question,
            missed,
        ])
    ui.emit_table(
        title=None,
        columns=["", "Question", "Missed keywords"],
        rows=rows,
    )

    rate_pct = report.pass_rate * 100
    color = "green" if rate_pct >= 80 else "yellow" if rate_pct >= 50 else "red"
    ui.out().print()
    ui.out().print(
        f"[{color}]{report.passed}/{report.total} passed "
        f"({rate_pct:.0f}%)[/]"
    )
    if comparison is not None:
        baseline_pct = baseline_report.pass_rate * 100
        lift_pct = comparison.lift * 100
        lift_color = "green" if lift_pct > 0 else "yellow" if lift_pct == 0 else "red"
        ui.out().print(
            f"[dim]Baseline (no retrieval): "
            f"{baseline_report.passed}/{baseline_report.total} "
            f"({baseline_pct:.0f}%)[/]"
        )
        sign = "+" if lift_pct > 0 else ""
        ui.out().print(
            f"[{lift_color}]Lift from class materials: "
            f"{sign}{lift_pct:.0f} points[/]  "
            f"[dim]({comparison.axiom_only_wins} only Axiom, "
            f"{comparison.baseline_only_wins} only baseline)[/]"
        )
    if report.failed:
        ui.out().print(
            "[dim]Failures usually mean the class corpus doesn't yet cover "
            "those topics — add materials via "
            "[/][cyan]axi classroom prep corpus[/]"
        )

    # CI gate: if --min-pass-rate is set, render a visible pass/fail line.
    if args.min_pass_rate is not None:
        gate_passed = report.pass_rate >= args.min_pass_rate
        threshold_pct = args.min_pass_rate * 100
        ui.out().print()
        if gate_passed:
            ui.emit_success(
                f"CI gate: pass rate {rate_pct:.0f}% "
                f">= threshold {threshold_pct:.0f}%"
            )
        else:
            ui.emit_error(
                f"CI gate FAILED: pass rate {rate_pct:.0f}% "
                f"< threshold {threshold_pct:.0f}%"
            )

    _cleanup_transient(transient_dir)
    return _evals_exit_code(report, args.min_pass_rate)


def _evals_exit_code(report, min_pass_rate: float | None) -> int:
    """CI-friendly exit codes.

    - ``--min-pass-rate`` NOT set: exit 1 iff any question failed
      (conservative default for interactive use).
    - ``--min-pass-rate`` SET: exit 1 iff pass rate strictly below
      the threshold — allows corpus-under-construction states while
      still trapping regressions.
    """
    if min_pass_rate is not None:
        return 0 if report.pass_rate >= min_pass_rate else 1
    return 0 if report.failed == 0 else 1


def _cleanup_transient(path: Path | None) -> None:
    """Best-effort rmtree for the evals-transient-index dir."""
    if path is None:
        return
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _cmd_briefs_generate(args: argparse.Namespace) -> int:
    """Instructor: regenerate per-student briefs for everyone in the cohort.

    Pulls from the coordinator's interaction store + cohort roster;
    invokes the LLM gateway best-effort per student. Each result lands
    in draft status — instructor must `briefs review --approve` before
    students see them.
    """
    from . import ui
    from .classroom_interaction import ClassroomInteractionStore
    from .coordinator_cohort_store import CohortNotFoundError, FileCohortStore
    from .student_briefs import BriefStore, generate_brief

    if not _require_active(args.classroom_id):
        return 1
    coord_dir = Path.home() / ".axi" / "coordinator"
    cohort_store = FileCohortStore(coord_dir)
    try:
        cohort = cohort_store.load(args.classroom_id)
    except CohortNotFoundError:
        ui.emit_error(
            f"No class \"{args.classroom_id}\" on this machine. "
            f"Run: axi classroom invite {args.classroom_id} --coordinator-url URL"
        )
        return 1

    classroom_dir = coord_dir / "classrooms" / args.classroom_id
    interactions = ClassroomInteractionStore(classroom_dir).list()
    brief_store = BriefStore(classroom_dir)

    def _llm(prompt: str, *, system: str = "") -> str | None:
        from axiom.infra.gateway import Gateway

        gw = Gateway()
        resp = gw.complete(
            prompt=prompt, system=system, task="classroom_brief",
        )
        if not getattr(resp, "success", False):
            return None
        return getattr(resp, "text", None) or None

    generated: list[str] = []
    for member in cohort.members:
        brief = generate_brief(
            student_id=member.student_id,
            classroom_id=args.classroom_id,
            interactions=interactions,
            llm=_llm,
        )
        brief_store.save(brief)
        generated.append(member.student_id)

    if args.json:
        print(json.dumps({
            "classroom_id": args.classroom_id,
            "generated_for": generated,
            "count": len(generated),
        }, indent=2))
        return 0

    if not generated:
        ui.emit_info(
            "No enrolled students yet — nothing to brief on. "
            "Invite students first."
        )
        return 0
    ui.emit_success(
        f"Generated {len(generated)} draft brief"
        f"{'' if len(generated) == 1 else 's'} for \"{args.classroom_id}\"."
    )
    ui.emit_next_steps([
        f"axi classroom briefs list {args.classroom_id}",
        f"axi classroom briefs review {args.classroom_id} <student_id> --approve",
    ], title="Review + approve before students see them")
    return 0


def _cmd_briefs_list(args: argparse.Namespace) -> int:
    """Show all students with briefs + their review status."""
    from . import ui
    from .student_briefs import BriefStore

    coord_dir = Path.home() / ".axi" / "coordinator"
    brief_store = BriefStore(coord_dir / "classrooms" / args.classroom_id)
    student_ids = brief_store.list_student_ids()

    rows = []
    for sid in student_ids:
        latest = brief_store.latest_for_student(sid)
        if latest is None:
            continue
        status = latest.review_status
        note = latest.instructor_note[:40] if latest.instructor_note else "—"
        rows.append([sid, status, _friendly_expiry(latest.generated_at), note])

    if args.json:
        print(json.dumps({
            "classroom_id": args.classroom_id,
            "briefs": [
                {"student_id": r[0], "status": r[1],
                 "generated_at": r[2], "note": r[3]}
                for r in rows
            ],
        }, indent=2))
        return 0

    if not rows:
        ui.emit_info(
            f"No briefs generated for \"{args.classroom_id}\" yet. "
            f"Run: axi classroom briefs generate {args.classroom_id}"
        )
        return 0
    ui.emit_table(
        title=f"Briefs for {args.classroom_id}",
        columns=["Student", "Status", "Generated", "Note"],
        rows=rows,
    )
    drafts = sum(1 for r in rows if r[1] == "draft")
    if drafts:
        ui.out().print()
        ui.emit_info(
            f"{drafts} draft brief{'' if drafts == 1 else 's'} "
            "awaiting your review."
        )
    return 0


def _cmd_briefs_review(args: argparse.Namespace) -> int:
    """Show + optionally curate a specific student's latest brief."""
    from . import ui
    from .student_briefs import BriefStore

    coord_dir = Path.home() / ".axi" / "coordinator"
    brief_store = BriefStore(coord_dir / "classrooms" / args.classroom_id)
    brief = brief_store.latest_for_student(args.student_id)

    if brief is None:
        ui.emit_error(
            f"No brief for {args.student_id!r} in \"{args.classroom_id}\". "
            f"Generate first: axi classroom briefs generate {args.classroom_id}"
        )
        return 1

    # Apply curation if requested.
    if args.approve or args.note:
        brief_store.approve(
            args.student_id, brief.generated_at,
            note=args.note or brief.instructor_note,
        )
        brief = brief_store.latest_for_student(args.student_id)

    if args.json:
        from dataclasses import asdict as _asdict
        print(json.dumps(_asdict(brief), indent=2))
        return 0

    ui.out().print()
    ui.emit_kv(
        f"Brief for {args.student_id}",
        {
            "Class": brief.classroom_id,
            "Generated": _friendly_expiry(brief.generated_at),
            "Status": brief.review_status,
        },
    )
    _render_brief_sections(brief)
    if brief.instructor_note:
        ui.out().print()
        ui.out().print(f"[bold]Instructor note:[/] {brief.instructor_note}")

    if not (args.approve or args.note):
        ui.out().print()
        ui.emit_next_steps([
            f"axi classroom briefs review {args.classroom_id} {args.student_id} --approve",
            f"axi classroom briefs review {args.classroom_id} {args.student_id} --note 'well done'",
        ], title="Approve or add a note")
    elif args.approve:
        ui.out().print()
        ui.emit_success("Approved. The student will see this on `axi classroom me`.")
    return 0


def _cmd_me(args: argparse.Namespace) -> int:
    """Student-side: fetch + render my own latest approved brief.

    With ``--memory``, switch to a memory-transparency view that shows
    what the coordinator has logged about my activity (question count,
    modes used, recent topics). With ``--forget <id>``, retract a
    specific interaction. The point is to make the coordinator's
    memory of the student both legible AND controllable by the student.
    """
    from . import ui

    # Try student-side cache first (populated by GET /classroom/briefs).
    class_dir = Path.home() / ".axi" / "classrooms" / args.classroom_id
    if not class_dir.is_dir():
        ui.emit_error(
            f"You're not in class \"{args.classroom_id}\" yet. "
            "Ask your instructor for the invite, then:\n"
            f"  axi classroom join <invite-from-your-instructor>"
        )
        return 1

    if getattr(args, "forget", None):
        return _forget_interaction(args, class_dir)

    if getattr(args, "memory", False):
        return _show_memory_view(args, class_dir)

    brief = _fetch_or_cache_my_brief(args.classroom_id, class_dir)
    if brief is None:
        if args.json:
            print(json.dumps({"brief": None, "message": "no brief yet"}))
        else:
            ui.emit_info(
                "No brief released yet for you in this class. Your "
                "instructor generates briefs periodically — check back "
                "after they've reviewed your activity."
            )
        return 0

    if args.json:
        from dataclasses import asdict as _asdict
        print(json.dumps(_asdict(brief), indent=2))
        return 0

    ui.out().print()
    ui.emit_kv(
        f"Your brief for {args.classroom_id}",
        {
            "Generated": _friendly_expiry(brief.generated_at),
        },
    )
    _render_brief_sections(brief)
    if brief.instructor_note:
        ui.out().print()
        ui.out().print(f"[bold]Note from your instructor:[/] {brief.instructor_note}")
    return 0


def _show_memory_view(args: argparse.Namespace, class_dir: Path) -> int:
    """Render the coordinator's memory of the student back to them.

    Fetches GET /classroom/memory/{student_id} and prints a flat,
    human-readable summary. This is the transparency surface — what
    the coordinator has on file, in the student's own words.
    """
    import urllib.error
    import urllib.request

    from . import ui
    from .student_membership import (
        MembershipNotFoundError,
        MembershipStore,
    )

    try:
        stored = MembershipStore(
            base_dir=Path.home() / ".axi"
        ).load(args.classroom_id)
        student_id = stored.student_id
    except MembershipNotFoundError:
        ui.emit_error(
            f"You're not in class \"{args.classroom_id}\" yet. "
            "Ask your instructor for the invite, then:\n"
            f"  axi classroom join <invite-from-your-instructor>"
        )
        return 1

    url_sidecar = class_dir / "coordinator_url.txt"
    if not url_sidecar.is_file():
        ui.emit_error(
            "No coordinator URL on file for this class. The "
            "transparency view needs the coordinator to be reachable."
        )
        return 1

    base_url = url_sidecar.read_text().strip().rstrip("/")
    target = base_url + f"/classroom/memory/{urllib_quote(student_id)}"
    try:
        with urllib.request.urlopen(target, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        ui.emit_error(
            f"Couldn't fetch your memory view from the classroom server: "
            f"{exc}"
        )
        return 1

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    ui.out().print()
    overview = {
        "Questions logged": str(data.get("question_count", 0)),
        "Answered": str(data.get("answered_count", 0)),
        "Unanswered": str(data.get("unanswered_count", 0)),
    }
    forgotten = data.get("forgotten_count", 0)
    if forgotten:
        overview["Retracted"] = str(forgotten)
    ui.emit_kv(
        f"What's on file for you in {args.classroom_id}",
        overview,
    )

    modes = data.get("modes_used") or {}
    if modes:
        ui.out().print()
        ui.out().print("[bold]Modes used[/]")
        for mode, count in sorted(modes.items(), key=lambda kv: -kv[1]):
            ui.out().print(f"  {mode}  × {count}")

    topics = data.get("topics") or []
    if topics:
        ui.out().print()
        ui.out().print("[bold]Topics in your questions[/]")
        for word, count in topics:
            ui.out().print(f"  {word}  × {count}")

    recents = data.get("recent_questions") or []
    if recents:
        ui.out().print()
        ui.out().print("[bold]Recent questions on file[/]")
        for r in recents:
            answered_marker = "✓" if r.get("had_answer") else "·"
            mode = r.get("mode") or "ask"
            question = r.get("question", "")
            iid = r.get("interaction_id", "")
            ui.out().print(
                f"  [dim]{answered_marker}[/] [{mode}] {question}  "
                f"[dim]({iid})[/]"
            )
        ui.out().print()
        ui.out().print(
            "[dim]To retract any of these, copy the id in parens and run:[/]"
        )
        ui.out().print(
            f"[dim]  axi classroom me {args.classroom_id} "
            "--forget <id>[/]"
        )

    if data.get("question_count", 0) == 0:
        ui.out().print()
        ui.emit_info(
            "Nothing logged yet. The coordinator only stores what you "
            "send via `axi classroom ask`."
        )
    return 0


def _forget_interaction(args: argparse.Namespace, class_dir: Path) -> int:
    """Student-side: retract a specific interaction from the coordinator.

    Hits DELETE /classroom/memory/{student_id}/{interaction_id}. Append-
    only JSONL means the coordinator writes a tombstone — the original
    line stays on disk for audit, but the content is filtered out of
    every read after that.
    """
    import urllib.error
    import urllib.request

    from . import ui
    from .student_membership import (
        MembershipNotFoundError,
        MembershipStore,
    )

    try:
        stored = MembershipStore(
            base_dir=Path.home() / ".axi"
        ).load(args.classroom_id)
        student_id = stored.student_id
    except MembershipNotFoundError:
        ui.emit_error(
            f"You're not in class \"{args.classroom_id}\" yet."
        )
        return 1

    url_sidecar = class_dir / "coordinator_url.txt"
    if not url_sidecar.is_file():
        ui.emit_error(
            "No coordinator URL on file for this class. Retraction "
            "needs the coordinator to be reachable."
        )
        return 1

    base_url = url_sidecar.read_text().strip().rstrip("/")
    target = (
        base_url
        + f"/classroom/memory/{urllib_quote(student_id)}"
        + f"/{urllib_quote(args.forget)}"
    )
    req = urllib.request.Request(target, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            ui.emit_error(
                f"No interaction {args.forget!r} on file for you. "
                f"Run `axi classroom me {args.classroom_id} --memory` "
                "to see your current interaction ids."
            )
        else:
            ui.emit_error(f"Retraction failed (HTTP {exc.code}).")
        return 1
    except (urllib.error.URLError, OSError) as exc:
        ui.emit_error(
            f"Couldn't reach the classroom server: {exc}"
        )
        return 1

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    if data.get("idempotent"):
        ui.emit_info(
            f"Interaction {args.forget!r} was already retracted. "
            "No change."
        )
    else:
        ui.emit_success(
            f"Retracted interaction {args.forget!r}. The coordinator "
            "will not surface this in future briefs or memory views."
        )
    return 0


def _fetch_or_cache_my_brief(
    classroom_id: str,
    class_dir: Path,
):
    """Try the coordinator first (fresh data), fall back to local cache."""
    import urllib.error
    import urllib.request

    from .student_membership import (
        MembershipNotFoundError,
        MembershipStore,
    )

    # Need my student_id from the membership manifest.
    try:
        stored = MembershipStore(base_dir=Path.home() / ".axi").load(classroom_id)
        student_id = stored.student_id
    except MembershipNotFoundError:
        return None

    url_sidecar = class_dir / "coordinator_url.txt"
    brief_cache = class_dir / "my_brief.json"

    if url_sidecar.is_file():
        base_url = url_sidecar.read_text().strip()
        target = (
            base_url.rstrip("/")
            + f"/classroom/briefs/{urllib_quote(student_id)}"
        )
        try:
            with urllib.request.urlopen(target, timeout=3.0) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8", errors="replace")
                    brief_cache.parent.mkdir(parents=True, exist_ok=True)
                    brief_cache.write_text(body)
                    data = json.loads(body)
                    return _brief_from_wire(data)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass

    # Offline fallback.
    if brief_cache.is_file():
        try:
            data = json.loads(brief_cache.read_text())
        except json.JSONDecodeError:
            return None
        return _brief_from_wire(data)

    return None


def _brief_from_wire(data: dict):
    from .student_briefs import StudentBrief

    if not data or not isinstance(data, dict):
        return None
    if "student_id" not in data or "classroom_id" not in data:
        return None
    return StudentBrief(
        student_id=str(data["student_id"]),
        classroom_id=str(data["classroom_id"]),
        period_start=str(data.get("period_start", "")),
        period_end=str(data.get("period_end", "")),
        generated_at=str(data["generated_at"]),
        sections=dict(data.get("sections") or {}),
        review_status=str(data.get("review_status", "draft")),
        instructor_note=str(data.get("instructor_note", "")),
    )


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


def _render_brief_sections(brief) -> None:
    """Shared rendering for instructor-review + student-me views."""
    from . import ui

    sections = brief.sections
    activity = sections.get("activity_summary")
    if activity:
        ui.out().print()
        ui.out().print("[bold]Activity[/]")
        ui.out().print(f"  {activity}")

    narrative = sections.get("narrative")
    if narrative:
        ui.out().print()
        ui.out().print("[bold]Reflection[/]")
        ui.out().print(f"  {narrative}")

    mode_mix = sections.get("mode_mix") or {}
    if mode_mix:
        ui.out().print()
        ui.out().print("[bold]Mode usage[/]")
        for m, n in sorted(mode_mix.items(), key=lambda kv: -kv[1]):
            ui.out().print(f"  [cyan]{m}[/]  [dim]× {n}[/]")

    unanswered = sections.get("unanswered_questions") or []
    if unanswered:
        ui.out().print()
        ui.out().print("[bold]Questions without an answer yet[/]")
        for q in unanswered[:5]:
            ui.out().print(f"  [dim]•[/] {q}")

    next_prompts = sections.get("suggested_next_prompts") or []
    if next_prompts:
        ui.out().print()
        ui.out().print("[bold]Try next[/]")
        for p in next_prompts:
            ui.out().print(f"  [cyan]{p}[/]")


def _cmd_quiz_broadcast(args: argparse.Namespace) -> int:
    """Instructor: build a quiz from a shipped bank and push to cohort."""
    from . import ui
    from .broadcast_quizzes import (
        BroadcastedQuiz,
        QuizQuestion,
        QuizStore,
        new_quiz_id,
    )
    from .classroom_evals import load_bank
    from .classroom_threads import now_iso

    # Lifecycle gate first — archive is a class-level invariant. If the
    # classroom is archived, that's the relevant error, not "this machine
    # isn't the coordinator."
    if not _require_active(args.classroom_id):
        return 1

    if _detect_classroom_role(args.classroom_id) != "instructor":
        ui.emit_error(
            f"Class \"{args.classroom_id}\" isn't on this machine. "
            "Only the instructor running the coordinator can broadcast quizzes."
        )
        return 1

    banks_dir = Path(__file__).parent / "banks"

    # `--bank-preset list` is the documented affordance for discovering
    # what's shipped (the help text promises it). Treat it as a listing
    # command rather than looking up a preset literally named "list".
    if args.bank_preset == "list":
        available = sorted(p.stem for p in banks_dir.glob("*.jsonl"))
        if not available:
            ui.emit_info("No quiz banks shipped with this build.")
        else:
            ui.emit_info(
                "Available quiz banks: " + ", ".join(available)
            )
        return 0

    preset_name = (args.bank_preset or "").replace("-", "_")
    bank_path = banks_dir / f"{preset_name}.jsonl"
    if not bank_path.is_file():
        available = sorted(p.stem for p in banks_dir.glob("*.jsonl"))
        ui.emit_error(
            f"No bank preset '{args.bank_preset}'. "
            f"Available: {', '.join(available) if available else '(none)'}"
        )
        return 1
    bank = load_bank(bank_path)

    pool = bank.questions
    if args.category:
        pool = [q for q in pool if q.category == args.category]
    if not pool:
        ui.emit_error(
            f"No questions match category {args.category!r} in bank "
            f"'{args.bank_preset}'. "
            f"Try `axi classroom quiz broadcast {args.classroom_id} "
            "--bank-preset ne101-core` without --category."
        )
        return 1

    # Shuffle + take N. Deterministic-ish via secrets so testable but
    # not predictable to students.
    import random
    rng = random.Random()
    picks = pool[:]
    rng.shuffle(picks)
    picks = picks[:args.questions]

    quiz_questions = [
        QuizQuestion(
            question_text=q.question,
            expected_keywords=list(q.expected_keywords),
            category=q.category,
        )
        for q in picks
    ]

    coord_dir = (
        Path.home() / ".axi" / "coordinator" / "classrooms" / args.classroom_id
    )
    quiz_store = QuizStore(coord_dir)
    quiz = BroadcastedQuiz(
        quiz_id=new_quiz_id(),
        classroom_id=args.classroom_id,
        created_at=now_iso(),
        created_by=_infer_default_owner(),
        topic=args.topic,
        questions=quiz_questions,
    )
    quiz_store.save(quiz)

    if args.json:
        print(json.dumps({
            "quiz_id": quiz.quiz_id,
            "classroom_id": quiz.classroom_id,
            "question_count": len(quiz.questions),
            "topic": quiz.topic,
        }, indent=2))
        return 0

    topic_blurb = f" on [cyan]{args.topic}[/]" if args.topic else ""
    ui.emit_success(
        f"Broadcast quiz{topic_blurb}: "
        f"{len(quiz.questions)} question"
        f"{'' if len(quiz.questions) == 1 else 's'} "
        f"from {args.bank_preset}. "
        f"Quiz id: [bold]{quiz.quiz_id}[/]"
    )
    ui.emit_info(
        "Students will see it on their next `axi classroom quiz pending`."
    )
    return 0


def _cmd_quiz_pending(args: argparse.Namespace) -> int:
    """Student: list quizzes the instructor pushed that I haven't taken."""
    import urllib.error
    import urllib.parse as _up
    import urllib.request

    from . import ui

    role = _detect_classroom_role(args.classroom_id)
    if role != "student":
        ui.emit_error(
            "This is a student command. Join the class first: "
            "axi classroom join <invite>"
        )
        return 1

    student_id = _student_id_from_membership(args.classroom_id)
    base_url = _coordinator_base_url(args.classroom_id)
    if not student_id or not base_url:
        ui.emit_error("Missing membership or coordinator URL.")
        return 1

    target = (
        base_url.rstrip("/")
        + "/classroom/quizzes/pending?student="
        + _up.quote(student_id, safe="")
    )
    try:
        with urllib.request.urlopen(target, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        ui.emit_error(f"Couldn't reach the classroom server: {exc}")
        return 1

    quizzes = data.get("quizzes", [])
    if args.json:
        print(json.dumps({"quizzes": quizzes}, indent=2))
        return 0

    if not quizzes:
        ui.emit_info(
            "No quizzes pending. Your instructor will push one when "
            "they want to check in."
        )
        return 0
    rows = [
        [
            q["quiz_id"],
            q.get("topic") or "—",
            _friendly_expiry(q["created_at"]),
            str(len(q.get("questions") or [])),
        ]
        for q in quizzes
    ]
    ui.emit_table(
        title="Pending quizzes",
        columns=["ID", "Topic", "Posted", "Questions"],
        rows=rows,
    )
    ui.out().print()
    ui.emit_next_steps([
        f"axi classroom quiz take {args.classroom_id} <quiz_id>",
    ], title="To take one")
    return 0


def _cmd_quiz_take(args: argparse.Namespace) -> int:
    """Student: fetch a quiz, prompt for each answer, submit.

    Closed-book by design — no retrieval, no LLM. The `quiz` learning
    mode's semantics enforced at the CLI level.
    """
    import urllib.error
    import urllib.request

    from . import ui

    role = _detect_classroom_role(args.classroom_id)
    if role != "student":
        ui.emit_error("Only a student can take a quiz.")
        return 1

    student_id = _student_id_from_membership(args.classroom_id)
    base_url = _coordinator_base_url(args.classroom_id)
    if not student_id or not base_url:
        ui.emit_error("Missing membership or coordinator URL.")
        return 1

    # Fetch the quiz.
    fetch_url = base_url.rstrip("/") + f"/classroom/quizzes/{args.quiz_id}"
    try:
        with urllib.request.urlopen(fetch_url, timeout=5.0) as resp:
            quiz = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            ui.emit_error(f"No quiz {args.quiz_id!r} on the classroom server.")
        else:
            ui.emit_error(f"Couldn't fetch quiz (HTTP {exc.code}).")
        return 1
    except (urllib.error.URLError, OSError) as exc:
        ui.emit_error(f"Couldn't reach the classroom server: {exc}")
        return 1

    questions = quiz.get("questions", [])
    if not questions:
        ui.emit_info("That quiz has no questions.")
        return 0

    # Prompt each question. In --json mode, route prompts to stderr so
    # stdout stays parseable; in interactive mode they go to stdout as
    # the student's normal display.
    topic = quiz.get("topic")
    prompt_console = ui.err() if args.json else ui.out()
    prompt_console.print()
    if topic:
        prompt_console.print(
            f"[bold]Quiz[/] [dim]on[/] [cyan]{topic}[/]  "
            f"[dim]({len(questions)} questions — closed book)[/]"
        )
    else:
        prompt_console.print(
            f"[bold]Quiz[/]  "
            f"[dim]({len(questions)} questions — closed book)[/]"
        )
    prompt_console.print()

    answers_payload = []
    for i, q in enumerate(questions):
        prompt_console.print(f"[bold]Q{i + 1}.[/] {q['question_text']}")
        # Write a visible input marker only in interactive mode — in
        # --json mode we need stdout pristine for the final JSON.
        if not args.json:
            sys.stdout.write("> ")
            sys.stdout.flush()
        line = sys.stdin.readline()
        answer = line.strip() if line else ""
        answers_payload.append({
            "question_index": i,
            "answer_text": answer,
        })
        prompt_console.print()

    # Submit.
    submit_url = (
        base_url.rstrip("/") + f"/classroom/quizzes/{args.quiz_id}/submit"
    )
    body = json.dumps({
        "student_id": student_id,
        "answers": answers_payload,
    }).encode("utf-8")
    req = urllib.request.Request(
        submit_url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        ui.emit_error(f"Couldn't submit: {exc}")
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    score_pct = result.get("score", 0.0) * 100
    passed = sum(
        1 for p in result.get("per_question", [])
        if p.get("passed")
    )
    color = "green" if score_pct >= 80 else "yellow" if score_pct >= 50 else "red"
    ui.out().print(
        f"[{color}]You got {passed}/{len(questions)} ({score_pct:.0f}%)[/]"
    )
    # Show which questions needed work.
    for i, pq in enumerate(result.get("per_question", [])):
        if not pq.get("passed"):
            missed = ", ".join(pq.get("missed_keywords", [])) or "—"
            ui.out().print(
                f"  [red]✗[/] Q{i + 1}: missing [dim]{missed}[/]"
            )
    return 0


def _cmd_quiz_results(args: argparse.Namespace) -> int:
    """Instructor: summary of submissions + per-student scores."""
    from . import ui
    from .broadcast_quizzes import QuizStore, score_submission

    if _detect_classroom_role(args.classroom_id) != "instructor":
        ui.emit_error(
            f"Class \"{args.classroom_id}\" isn't on this machine."
        )
        return 1

    coord_dir = (
        Path.home() / ".axi" / "coordinator" / "classrooms" / args.classroom_id
    )
    store = QuizStore(coord_dir)
    quiz = store.get(args.quiz_id)
    if quiz is None:
        ui.emit_error(f"No quiz {args.quiz_id!r} in \"{args.classroom_id}\".")
        return 1

    subs = store.submissions_for_quiz(args.quiz_id)
    scored = []
    for s in subs:
        _, rate = score_submission(quiz, s)
        scored.append((s.student_id, rate, s.submitted_at))
    scored.sort(key=lambda row: -row[1])

    if args.json:
        print(json.dumps({
            "quiz_id": quiz.quiz_id,
            "topic": quiz.topic,
            "question_count": len(quiz.questions),
            "submission_count": len(scored),
            "submissions": [
                {
                    "student_id": sid,
                    "score": rate,
                    "submitted_at": ts,
                }
                for sid, rate, ts in scored
            ],
        }, indent=2))
        return 0

    ui.out().print()
    ui.out().print(
        f"[bold]Quiz results[/]  [dim]{quiz.topic or ''}[/]  "
        f"[dim]({len(scored)} submission{'' if len(scored) == 1 else 's'})[/]"
    )
    if not scored:
        ui.out().print()
        ui.emit_info("No one's taken it yet.")
        return 0
    rows = [
        [
            sid,
            f"{rate * 100:.0f}%",
            _friendly_expiry(ts),
        ]
        for sid, rate, ts in scored
    ]
    ui.emit_table(
        title=None,
        columns=["Student", "Score", "Submitted"],
        rows=rows,
    )
    avg = sum(row[1] for row in scored) / len(scored)
    ui.out().print()
    ui.out().print(f"[dim]Class average:[/] [bold]{avg * 100:.0f}%[/]")
    return 0


def _detect_classroom_role(classroom_id: str) -> str:
    """Return "instructor" | "student" | "unknown" based on local state."""
    coord_dir = (
        Path.home() / ".axi" / "coordinator" / "classrooms" / classroom_id
    )
    if coord_dir.is_dir():
        return "instructor"
    student_dir = Path.home() / ".axi" / "classrooms" / classroom_id
    if (student_dir / "membership.json").is_file():
        return "student"
    return "unknown"


def _student_id_from_membership(classroom_id: str) -> str | None:
    from .student_membership import MembershipNotFoundError, MembershipStore

    try:
        stored = MembershipStore(base_dir=Path.home() / ".axi").load(classroom_id)
        return stored.student_id
    except MembershipNotFoundError:
        return None


def _coordinator_base_url(classroom_id: str) -> str | None:
    sidecar = (
        Path.home() / ".axi" / "classrooms" / classroom_id / "coordinator_url.txt"
    )
    if not sidecar.is_file():
        return None
    val = sidecar.read_text().strip()
    return val or None


def _cmd_ask_instructor(args: argparse.Namespace) -> int:
    """Student-side: open a new thread with the instructor."""
    import urllib.error
    import urllib.request

    from . import ui

    role = _detect_classroom_role(args.classroom_id)
    if role != "student":
        ui.emit_error(
            f"You don't seem to be a student in class \"{args.classroom_id}\". "
            "Run: axi classroom join <invite-from-your-instructor>"
        )
        return 1

    student_id = _student_id_from_membership(args.classroom_id)
    base_url = _coordinator_base_url(args.classroom_id)
    if not student_id or not base_url:
        ui.emit_error(
            "Missing membership or coordinator URL. "
            "Try rejoining: axi classroom join <invite>"
        )
        return 1

    body = json.dumps({
        "student_id": student_id,
        "opened_by": "student",
        "author_id": student_id,
        "text": args.message,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/classroom/threads/open",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        ui.emit_error(
            f"Couldn't reach the classroom server: {exc}. Try again when "
            "you're online."
        )
        return 1

    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    ui.emit_success(
        f"Question sent to your instructor. Thread id: [bold]{data['thread_id']}[/]"
    )
    ui.emit_next_steps([
        f"axi classroom threads {args.classroom_id}",
        f"axi classroom reply {args.classroom_id} {data['thread_id']} \"<your follow-up>\"",
    ])
    return 0


def _cmd_ask_student(args: argparse.Namespace) -> int:
    """Instructor-side: open a thread with a specific student (local write)."""
    from . import ui
    from .classroom_threads import (
        Thread,
        ThreadMessage,
        ThreadStore,
        new_thread_id,
        now_iso,
    )

    if _detect_classroom_role(args.classroom_id) != "instructor":
        ui.emit_error(
            f"Class \"{args.classroom_id}\" isn't on this machine. "
            "Only an instructor running the coordinator can ask a student."
        )
        return 1

    coord_dir = (
        Path.home() / ".axi" / "coordinator" / "classrooms" / args.classroom_id
    )
    store = ThreadStore(coord_dir)
    ts = now_iso()
    thread = Thread(
        thread_id=new_thread_id(),
        classroom_id=args.classroom_id,
        student_id=args.student_id,
        opened_by="instructor",
        status="open",
        opened_at=ts,
        messages=[ThreadMessage(
            author_role="instructor",
            author_id=_infer_default_owner(),
            text=args.message,
            timestamp=ts,
        )],
    )
    store.save(thread)

    if args.json:
        from dataclasses import asdict as _asdict
        print(json.dumps({
            "thread_id": thread.thread_id,
            "classroom_id": thread.classroom_id,
            "student_id": thread.student_id,
            "opened_by": thread.opened_by,
            "status": thread.status,
            "opened_at": thread.opened_at,
            "messages": [_asdict(m) for m in thread.messages],
        }, indent=2))
        return 0
    ui.emit_success(
        f"Thread opened with {args.student_id}. "
        f"Id: [bold]{thread.thread_id}[/]"
    )
    ui.emit_info(
        "The student will see your question on their next "
        "`axi classroom threads`."
    )
    return 0


def _cmd_threads(args: argparse.Namespace) -> int:
    """List threads. Instructor sees all cohort threads; student sees own."""
    from . import ui

    role = _detect_classroom_role(args.classroom_id)
    if role == "instructor":
        return _threads_instructor_view(args)
    if role == "student":
        return _threads_student_view(args)

    ui.emit_error(
        f"You're not in class \"{args.classroom_id}\". "
        f"Join with: axi classroom join <invite>"
    )
    return 1


def _threads_instructor_view(args: argparse.Namespace) -> int:
    from . import ui
    from .classroom_threads import ThreadStore

    coord_dir = (
        Path.home() / ".axi" / "coordinator" / "classrooms" / args.classroom_id
    )
    store = ThreadStore(coord_dir)
    threads = store.list_open() if args.open_only else store.list_all()

    if args.json:
        print(json.dumps({
            "threads": [{
                "thread_id": t.thread_id,
                "student_id": t.student_id,
                "opened_by": t.opened_by,
                "status": t.status,
                "opened_at": t.opened_at,
                "message_count": len(t.messages),
            } for t in threads],
        }, indent=2))
        return 0

    if not threads:
        ui.emit_info("No threads yet. They show up here when students ask.")
        return 0
    rows = [
        [
            t.thread_id,
            t.student_id,
            t.status,
            _friendly_expiry(t.opened_at),
            str(len(t.messages)),
        ]
        for t in threads
    ]
    ui.emit_table(
        title=f"Threads for {args.classroom_id}",
        columns=["ID", "Student", "Status", "Opened", "Messages"],
        rows=rows,
    )
    return 0


def _threads_student_view(args: argparse.Namespace) -> int:
    """Student lists their own threads via coordinator HTTP."""
    import urllib.error
    import urllib.parse as _up
    import urllib.request

    from . import ui

    student_id = _student_id_from_membership(args.classroom_id)
    base_url = _coordinator_base_url(args.classroom_id)
    if not student_id or not base_url:
        ui.emit_error(
            "Missing membership or coordinator URL. "
            "Try rejoining: axi classroom join <invite>"
        )
        return 1

    target = (
        base_url.rstrip("/")
        + "/classroom/threads?student="
        + _up.quote(student_id, safe="")
    )
    try:
        with urllib.request.urlopen(target, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        ui.emit_error(f"Couldn't reach the classroom server: {exc}")
        return 1

    threads = data.get("threads", [])
    if args.open_only:
        threads = [t for t in threads if t.get("status") == "open"]

    if args.json:
        print(json.dumps({"threads": threads}, indent=2))
        return 0

    if not threads:
        ui.emit_info(
            "No threads yet. Open one with "
            f"`axi classroom ask-instructor {args.classroom_id} \"<question>\"`."
        )
        return 0
    rows = [
        [
            t["thread_id"],
            t["status"],
            _friendly_expiry(t["opened_at"]),
            str(len(t.get("messages", []))),
        ]
        for t in threads
    ]
    ui.emit_table(
        title=f"Your threads for {args.classroom_id}",
        columns=["ID", "Status", "Opened", "Messages"],
        rows=rows,
    )
    return 0


def _cmd_reply(args: argparse.Namespace) -> int:
    """Reply to a thread. Role-aware: instructor writes locally, student POSTs."""
    from . import ui

    role = _detect_classroom_role(args.classroom_id)
    if role == "instructor":
        return _reply_instructor(args)
    if role == "student":
        return _reply_student(args)

    ui.emit_error(
        f"You're not in class \"{args.classroom_id}\". "
        "Join with: axi classroom join <invite>"
    )
    return 1


def _reply_instructor(args: argparse.Namespace) -> int:
    from . import ui
    from .classroom_threads import ThreadMessage, ThreadStore, now_iso

    coord_dir = (
        Path.home() / ".axi" / "coordinator" / "classrooms" / args.classroom_id
    )
    store = ThreadStore(coord_dir)
    try:
        updated = store.reply(args.thread_id, ThreadMessage(
            author_role="instructor",
            author_id=_infer_default_owner(),
            text=args.message,
            timestamp=now_iso(),
        ))
    except KeyError:
        ui.emit_error(f"No thread {args.thread_id!r} in \"{args.classroom_id}\".")
        return 1
    except ValueError as exc:
        ui.emit_error(str(exc))
        return 1

    if args.json:
        print(json.dumps({
            "thread_id": updated.thread_id,
            "status": updated.status,
            "message_count": len(updated.messages),
        }))
        return 0
    ui.emit_success(
        f"Replied. Thread is now [bold]{updated.status}[/]; "
        f"{len(updated.messages)} message"
        f"{'' if len(updated.messages) == 1 else 's'} in thread."
    )
    return 0


def _reply_student(args: argparse.Namespace) -> int:
    import urllib.error
    import urllib.request

    from . import ui

    student_id = _student_id_from_membership(args.classroom_id)
    base_url = _coordinator_base_url(args.classroom_id)
    if not student_id or not base_url:
        ui.emit_error(
            "Missing membership or coordinator URL. "
            "Try rejoining: axi classroom join <invite>"
        )
        return 1

    body = json.dumps({
        "author_role": "student",
        "author_id": student_id,
        "text": args.message,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/")
        + f"/classroom/threads/{args.thread_id}/reply",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            ui.emit_error(f"No thread {args.thread_id!r} on the classroom server.")
        else:
            ui.emit_error(f"Couldn't reply (HTTP {exc.code}).")
        return 1
    except (urllib.error.URLError, OSError) as exc:
        ui.emit_error(f"Couldn't reach the classroom server: {exc}")
        return 1

    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    ui.emit_success(
        f"Sent. Thread is now [bold]{data['status']}[/]."
    )
    return 0


def _cmd_modes(args: argparse.Namespace) -> int:
    """Instructor: show or change the learning-mode policy for a class.

    Without flags, prints the current policy (which modes are allowed,
    whether one is forced) plus per-mode descriptions so the instructor
    can remember what each mode does. With ``--allow`` / ``--force``,
    mutates the policy and persists it on the cohort record — student
    ``axi classroom ask`` calls pick up the change via the
    ``/classroom/policy`` endpoint on their next invocation.
    """
    from . import ui
    from .coordinator_cohort_store import FileCohortStore
    from .learning_modes import (
        MODE_REGISTRY,
        ClassroomModePolicy,
        list_modes,
    )

    coord_dir = Path.home() / ".axi" / "coordinator"
    cohort_store = FileCohortStore(coord_dir)

    if not cohort_store.exists(args.classroom_id):
        ui.emit_error(
            f"No class \"{args.classroom_id}\" is set up on this machine yet. "
            f"Run: axi classroom invite {args.classroom_id} --coordinator-url URL"
        )
        return 1

    # Load or default the policy.
    raw = None
    try:
        raw = cohort_store.get_mode_policy(args.classroom_id)
    except Exception:
        raw = None
    policy = (
        ClassroomModePolicy.from_dict(raw)
        if raw is not None
        else ClassroomModePolicy.default()
    )

    # --- Mutations (if any) ---
    changed = False

    if args.allow is not None:
        if args.allow.lower() == "all":
            new_allowed = frozenset(MODE_REGISTRY)
        else:
            pieces = [p.strip() for p in args.allow.split(",") if p.strip()]
            unknown = [p for p in pieces if p not in MODE_REGISTRY]
            if unknown:
                ui.emit_error(
                    f"Unknown mode(s): {', '.join(unknown)}. "
                    f"Valid: {', '.join(sorted(MODE_REGISTRY))}"
                )
                return 1
            if not pieces:
                ui.emit_error("--allow needs at least one mode (or 'all').")
                return 1
            new_allowed = frozenset(pieces)
        policy = ClassroomModePolicy(
            allowed_modes=new_allowed,
            forced_mode=policy.forced_mode,
        )
        changed = True

    if args.force is not None:
        force = None if args.force.lower() == "none" else args.force
        if force is not None:
            if force not in MODE_REGISTRY:
                ui.emit_error(
                    f"Unknown mode '{force}'. "
                    f"Valid: {', '.join(sorted(MODE_REGISTRY))}"
                )
                return 1
            if force not in policy.allowed_modes:
                ui.emit_error(
                    f"Can't force '{force}' — it's not in the allowed "
                    "set. Add it first: "
                    f"axi classroom modes {args.classroom_id} --allow {force},..."
                )
                return 1
        policy = ClassroomModePolicy(
            allowed_modes=policy.allowed_modes,
            forced_mode=force,
        )
        changed = True

    if changed:
        cohort = cohort_store.load(args.classroom_id)
        cohort_store.save(cohort, mode_policy=policy.to_dict())

    # --- Report ---
    if args.json:
        print(json.dumps({
            "classroom_id": args.classroom_id,
            "policy": policy.to_dict(),
            "changed": changed,
        }, indent=2))
        return 0

    ui.out().print()
    ui.out().print(f"[bold]Modes for \"{args.classroom_id}\"[/]")
    if policy.forced_mode:
        ui.out().print(
            f"[yellow]Forced:[/] all students are in "
            f"[bold]{policy.forced_mode}[/] mode right now."
        )
    rows = []
    for m in list_modes():
        allowed = m.name in policy.allowed_modes
        glyph = "[green]✓[/]" if allowed else "[dim]✗[/]"
        forced_marker = "  [yellow](forced)[/]" if m.name == policy.forced_mode else ""
        rows.append([glyph, m.name + forced_marker, m.description])
    ui.emit_table(
        title=None,
        columns=["", "Mode", "What it does"],
        rows=rows,
    )
    if changed:
        ui.out().print()
        ui.emit_success("Policy updated. Students pick up the change on their next `ask`.")
    return 0


def _cmd_classroom_status(args: argparse.Namespace) -> int:
    """Instructor dashboard: list classes on this machine and who's joined.

    Two modes on one command:
    - No argument: summary table of every class, with student count.
    - With a classroom_id: drilldown showing members + status.
    """
    from . import ui
    from .coordinator_cohort_store import CohortNotFoundError, FileCohortStore
    from .coordinator_invite_registry import FileInviteRegistry

    coord_dir = Path.home() / ".axi" / "coordinator"
    store = FileCohortStore(coord_dir)
    registry = FileInviteRegistry(coord_dir / "invites.json")

    # Cohort drilldown.
    if args.classroom_id is not None:
        try:
            cohort = store.load(args.classroom_id)
        except CohortNotFoundError:
            if args.json:
                print(json.dumps({"error": f"no class {args.classroom_id!r}"}))
            else:
                ui.emit_serve_needs_bootstrap(args.classroom_id)
            return 1

        coord_url = store.get_coordinator_url(args.classroom_id)
        members = [
            {
                "student_id": m.student_id,
                "status": m.status,
                "joined_at": _friendly_expiry(m.joined_at) if m.joined_at else None,
            }
            for m in cohort.members
        ]

        joined_tokens = {m.invite_token for m in cohort.members}
        pending = [
            inv for inv in registry.list_for_classroom(args.classroom_id)
            if inv.token not in joined_tokens
            and not registry.is_consumed(inv.token)
        ]

        if args.json:
            print(json.dumps({
                "classroom_id": args.classroom_id,
                "coordinator_url": coord_url,
                "members": members,
                "pending_invites": len(pending),
            }, indent=2))
            return 0

        ui.emit_status_cohort_detail(
            classroom_id=args.classroom_id,
            coordinator_url=coord_url,
            members=members,
            pending_invites=len(pending),
        )
        return 0

    # Summary.
    ids = store.list_ids()
    rows = []
    for cid in ids:
        try:
            cohort = store.load(cid)
        except (CohortNotFoundError, ValueError):
            continue
        rows.append({
            "classroom_id": cid,
            "member_count": len(cohort.members),
            "coordinator_url": store.get_coordinator_url(cid),
        })

    if args.json:
        print(json.dumps({"classes": rows}, indent=2))
        return 0

    ui.emit_status_cohort_list(rows)
    return 0


def _cmd_archive(args: argparse.Namespace) -> int:
    from .archive import archive_classroom

    result = archive_classroom(
        classroom_id=args.classroom_id,
        archiver=args.archiver,
        reason=args.reason,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("archived") else 1

    from . import ui
    if result.get("archived"):
        marker = " (already archived)" if result.get("idempotent") else ""
        ui.emit_success(f"Archived classroom \"{args.classroom_id}\"{marker}.")
        ui.emit_kv("Details", {
            "archiver": result["archiver"],
            "archived at": _friendly_expiry(result["archived_at"]),
            "reason": result["reason"],
        })
        return 0
    ui.emit_blocked(
        what=f"archive classroom \"{args.classroom_id}\"",
        blockers=[result.get("error", "unknown error")],
    )
    return 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Run the classroom diagnostic and render the report.

    Read-only — never mutates state. Returns:
      0 — overall ``ok``
      1 — overall ``warn`` (non-fatal but worth attention)
      2 — overall ``fail`` (something downstream will break)
    """
    from . import ui
    from .doctor import run_diagnostics

    report = run_diagnostics(args.classroom_id)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return _doctor_exit_code(report.overall)

    glyph = {"ok": "[green]✓[/]", "warn": "[yellow]![/]", "fail": "[red]✗[/]"}
    ui.out().print()
    overall_glyph = glyph.get(report.overall, "·")
    ui.out().print(
        f"{overall_glyph} Classroom \"{report.classroom_id}\" "
        f"[dim](role: {report.role})[/]"
    )
    ui.out().print()
    for c in report.checks:
        marker = glyph.get(c.status, "·")
        ui.out().print(f"  {marker} [bold]{c.name}[/] — {c.detail}")
        if c.hint:
            ui.out().print(f"      [dim]{c.hint}[/]")
    ui.out().print()
    return _doctor_exit_code(report.overall)


def _doctor_exit_code(overall: str) -> int:
    return {"ok": 0, "warn": 1, "fail": 2}.get(overall, 0)


def _cmd_export(args: argparse.Namespace) -> int:
    """Instructor-side: bundle classroom state into a portable .tar.gz."""
    from . import ui
    from .conclusion import export_classroom

    result = export_classroom(
        classroom_id=args.classroom_id, out_path=args.out,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("exported") else 1

    if result.get("exported"):
        ui.emit_success(f"Exported classroom \"{args.classroom_id}\".")
        ui.emit_kv("Details", {
            "path": result["path"],
            "exported at": _friendly_expiry(result["exported_at"]),
        })
        return 0
    ui.emit_error(result.get("error", "export failed"))
    return 1


def _cmd_leave(args: argparse.Namespace) -> int:
    """Student-side: disconnect from a classroom on this machine.

    Removes the membership manifest and (by default) the indexed
    materials. Does not contact the coordinator — the instructor's
    roster is unaffected. Idempotent: leaving a class you're not in
    is a no-op + return 0.
    """
    from . import ui
    from .student_membership import MembershipStore

    class_dir = Path.home() / ".axi" / "classrooms" / args.classroom_id
    membership_store = MembershipStore(base_dir=Path.home() / ".axi")

    removed_membership = membership_store.delete(args.classroom_id)
    removed_other = False

    if class_dir.is_dir():
        if args.keep_materials:
            # Keep the indexed materials but drop the coordinator URL
            # sidecar + brief cache so the student can't accidentally
            # fetch new data after leaving.
            for fname in ("coordinator_url.txt", "my_brief.json"):
                fp = class_dir / fname
                if fp.is_file():
                    fp.unlink()
                    removed_other = True
        else:
            import shutil
            shutil.rmtree(class_dir, ignore_errors=True)
            removed_other = True

    removed_any = removed_membership or removed_other

    payload = {
        "left": True,
        "classroom_id": args.classroom_id,
        "removed_any": removed_any,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    if removed_any:
        msg = (
            f"Left class \"{args.classroom_id}\". "
            + (
                "Local materials kept."
                if args.keep_materials
                else "Local cache + materials removed."
            )
        )
        ui.emit_success(msg)
    else:
        ui.emit_info(
            f"Nothing to remove — you weren't in class "
            f"\"{args.classroom_id}\" on this machine."
        )
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    from . import ui
    from .publish import publish_classroom

    result = publish_classroom(
        classroom_id=args.classroom_id, approver=args.approver,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("published") else 1

    if result.get("published"):
        ui.emit_success(f"Published classroom \"{args.classroom_id}\".")
        ui.emit_kv("Details", {
            "approver": result["approver"],
            "published at": _friendly_expiry(result["published_at"]),
        })
        ui.emit_next_steps([
            f"axi classroom enroll {args.classroom_id} --instructor <you>",
        ])
        return 0

    ui.emit_blocked(
        what=f"publish classroom \"{args.classroom_id}\"",
        blockers=(
            result.get("blockers")
            or [result.get("error", "unknown error")]
        ),
        suggestion=f"axi classroom prep status {args.classroom_id}",
    )
    return 1


def _cmd_dry_run_enhanced(args: argparse.Namespace) -> int:
    from .publish import enhanced_dry_run

    result = enhanced_dry_run(
        classroom_id=args.classroom_id, queries=args.query or None,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if "error" not in result else 1
    from . import ui
    if "error" in result:
        ui.emit_error(result["error"])
        return 1
    ui.emit_info(f"Dry-run — {result['turns']} turn(s).")
    for i, turn in enumerate(result["transcript"], 1):
        ui.out().print()
        ui.out().print(f"  [bold][{i}] Q:[/] {turn.get('query', '')}")
        retrieved = turn.get("retrieved") or []
        if retrieved:
            ui.out().print(f"  [dim]Retrieved {len(retrieved)} doc(s):[/]")
            for doc in retrieved[:3]:
                title = doc.get("title") or doc.get("id") or "(untitled)"
                ui.out().print(f"    [dim]•[/] {title}")
        ui.out().print(f"  [bold]A:[/] {turn.get('response', '')}")
    return 0


def _cmd_lms_setup_list_providers(args: argparse.Namespace) -> int:
    from .lms_setup import list_providers

    providers = list_providers()
    if args.json:
        print(json.dumps({"providers": providers, "count": len(providers)}, indent=2))
        return 0

    from . import ui
    rows = [
        [p["id"], p["status"], p["display_name"]]
        for p in providers
    ]
    ui.emit_table(
        title="LMS providers",
        columns=["ID", "Status", "Provider"],
        rows=rows,
    )
    notes = [(p["id"], p["notes"]) for p in providers if p.get("notes")]
    if notes:
        ui.emit_kv("Notes", dict(notes))
    return 0


def _cmd_lms_setup_canvas_probe(args: argparse.Namespace) -> int:
    from .lms_setup import build_fake_canvas_for_cli, canvas_probe

    mock = build_fake_canvas_for_cli() if args.fake else None
    result = canvas_probe(
        instance_url=args.instance_url, token=args.token, mock_server=mock,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("connected") else 1

    from . import ui
    if result.get("connected"):
        ui.emit_success(f"Canvas at {args.instance_url} is reachable.")
        return 0
    ui.emit_error(
        f"Could not reach Canvas: {result.get('error', 'unknown error')}"
    )
    return 1


def _cmd_lms_setup_canvas_configure(args: argparse.Namespace) -> int:
    from .lms_setup import build_fake_canvas_for_cli, canvas_configure

    mock = build_fake_canvas_for_cli() if args.fake else None
    result = canvas_configure(
        classroom_id=args.classroom_id,
        instance_url=args.instance_url,
        token=args.token,
        canvas_course_id=args.canvas_course_id,
        mock_server=mock,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("configured") else 1

    from . import ui
    if result.get("configured"):
        ui.emit_success(
            f"Configured Canvas for classroom \"{args.classroom_id}\"."
        )
        ui.emit_kv("Details", {
            "course_id": result["canvas_course_id"],
            "roster": f"{result['roster_count']} student(s)",
        })
        roster_preview = result.get("roster_preview") or []
        if roster_preview:
            ui.emit_table(
                title="Roster preview",
                columns=["Name", "Email"],
                rows=[
                    [s.get("name", s["id"]), s.get("email", "")]
                    for s in roster_preview
                ],
            )
        return 0
    ui.emit_error(
        f"Configuration failed: {result.get('error', 'unknown error')}"
    )
    return 1


def _cmd_lms_setup_none(args: argparse.Namespace) -> int:
    from .lms_setup import mark_no_lms

    result = mark_no_lms(classroom_id=args.classroom_id)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("no_lms") else 1

    from . import ui
    if result.get("no_lms"):
        ui.emit_success(
            f"Classroom \"{args.classroom_id}\" will use a manual roster "
            "(no LMS)."
        )
        return 0
    ui.emit_error(
        f"Could not mark no-LMS: {result.get('error', 'unknown error')}"
    )
    return 1


def _cmd_rails_list_banks(args: argparse.Namespace) -> int:
    from .question_banks import list_banks

    banks = [b.to_dict() for b in list_banks()]
    if args.json:
        print(json.dumps({"banks": banks, "count": len(banks)}, indent=2))
        return 0

    from . import ui
    if not banks:
        ui.emit_info("No question banks registered yet.")
        return 0
    rows = [
        [
            b["id"],
            str(b["question_count"]),
            b["source"],
            (b.get("description") or "")[:60],
        ]
        for b in banks
    ]
    ui.emit_table(
        title=f"Question banks ({len(banks)})",
        columns=["ID", "Questions", "Source", "Description"],
        rows=rows,
    )
    return 0


def _cmd_rails_add(args: argparse.Namespace) -> int:
    from .question_banks import add_rail_from_bank

    loaded = _load_course_for_checkpoint_op(args.course_id)
    if loaded is None:
        msg = f"course {args.course_id!r} not found"
        if args.json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            from . import ui
            ui.emit_error(msg)
        return 1
    _, data = loaded
    manifest = dict(data.get("manifest") or {})
    # Ensure rails list exists
    manifest.setdefault("rails", list(data.get("rails") or []))

    ids = [q.strip() for q in (args.ids or "").split(",") if q.strip()] or None
    try:
        rail = add_rail_from_bank(
            manifest,
            rail_id=args.rail_id,
            bank_id=args.bank_id,
            question_ids=ids,
            auto_apply_to=args.auto_apply_to,
            required=not args.not_required,
        )
    except ValueError as e:
        if args.json:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            from . import ui
            ui.emit_error(str(e))
        return 1

    # Also persist the rails list on the top-level course record so the
    # legacy CoursePrepWorkflow (which reads data['rails']) sees it.
    _save_course_manifest_update(args.course_id, data, manifest)
    updated = dict(data)
    updated["manifest"] = manifest
    updated["rails"] = list(manifest.get("rails") or [])
    from .operational_store import _reg

    _reg().register(kind="course", name=args.course_id, data=updated)

    payload = {
        "course_id": args.course_id,
        "added": rail,
        "count": len(manifest.get("rails") or []),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    from . import ui
    q_ct = len(rail["questions"])
    ui.emit_success(
        f"Added rail \"{rail['id']}\" to \"{args.course_id}\" "
        f"(bank={rail['source']}, {q_ct} question(s))."
    )
    return 0


def _cmd_rails_edit(args: argparse.Namespace) -> int:
    from .rail_edit import edit_rail_via_editor

    result = edit_rail_via_editor(
        course_id=args.course_id, rail_id=args.rail_id,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("applied") else 1
    from . import ui
    if result.get("applied"):
        note = " (no changes)" if result.get("noop") else ""
        ui.emit_success(
            f"Edited rail \"{args.rail_id}\" on \"{args.course_id}\"{note}."
        )
        return 0
    ui.emit_error(result.get("error", "unknown error"))
    return 1


def _cmd_rails_preview(args: argparse.Namespace) -> int:
    from .question_banks import preview_rail

    loaded = _load_course_for_checkpoint_op(args.course_id)
    if loaded is None:
        msg = f"course {args.course_id!r} not found"
        if args.json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            from . import ui
            ui.emit_error(msg)
        return 1
    _, data = loaded
    manifest = data.get("manifest") or {}
    # rails may live either on the manifest or at the top-level data dict.
    if "rails" not in manifest:
        manifest = dict(manifest)
        manifest["rails"] = list(data.get("rails") or [])
    try:
        session = preview_rail(manifest, rail_id=args.rail_id)
    except ValueError as e:
        if args.json:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            from . import ui
            ui.emit_error(str(e))
        return 1

    if args.json:
        print(json.dumps(session, indent=2))
    else:
        print(
            f"Preview: rail={session['rail_id']} as {session['student_persona']}"
        )
        for turn in session["turns"]:
            print(f"  Q ({turn['question_id']}): {turn['prompt']}")
            print(f"    A: {turn['sample_response']}  [{turn['response_type']}]")
    return 0


def _cmd_checkpoints_list(args: argparse.Namespace) -> int:
    from .checkpoints import list_checkpoints

    loaded = _load_course_for_checkpoint_op(args.course_id)
    if loaded is None:
        msg = f"course {args.course_id!r} not found"
        if args.json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            from . import ui
            ui.emit_error(msg)
        return 1
    _, data = loaded
    manifest = data.get("manifest") or {}
    items = list_checkpoints(manifest)
    payload = {"course_id": args.course_id, "checkpoints": items, "count": len(items)}
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    from . import ui
    if not items:
        ui.emit_info(f"No checkpoints configured for \"{args.course_id}\".")
        return 0
    rows = [
        [
            cp.get("id", ""),
            cp.get("method", "quiz"),
            cp.get("timing", "-"),
            "required" if cp.get("required") else "optional",
        ]
        for cp in items
    ]
    ui.emit_table(
        title=f"Checkpoints for {args.course_id}",
        columns=["ID", "Method", "Timing", "Kind"],
        rows=rows,
    )
    return 0


def _cmd_checkpoints_add(args: argparse.Namespace) -> int:
    from .checkpoints import add_checkpoint

    loaded = _load_course_for_checkpoint_op(args.course_id)
    if loaded is None:
        msg = f"course {args.course_id!r} not found"
        if args.json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            from . import ui
            ui.emit_error(msg)
        return 1
    _, data = loaded
    manifest = dict(data.get("manifest") or {})
    try:
        added = add_checkpoint(
            manifest,
            {
                "id": args.checkpoint_id,
                "label": args.label or args.checkpoint_id,
                "timing": args.timing,
                "method": args.method,
                "questionnaire_id": args.questionnaire_id,
                "required": args.required,
            },
        )
    except ValueError as e:
        payload = {"error": str(e)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            from . import ui
            ui.emit_error(str(e))
        return 1
    _save_course_manifest_update(args.course_id, data, manifest)
    payload = {
        "course_id": args.course_id,
        "added": added,
        "count": len(manifest.get("checkpoints") or []),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    from . import ui
    ui.emit_success(
        f"Added checkpoint \"{added['id']}\" ({added['method']}, timing={added['timing']})."
    )
    return 0


def _cmd_checkpoints_remove(args: argparse.Namespace) -> int:
    from .checkpoints import remove_checkpoint

    loaded = _load_course_for_checkpoint_op(args.course_id)
    if loaded is None:
        msg = f"course {args.course_id!r} not found"
        if args.json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            from . import ui
            ui.emit_error(msg)
        return 1
    _, data = loaded
    manifest = dict(data.get("manifest") or {})
    removed = remove_checkpoint(manifest, args.checkpoint_id)
    if not removed:
        msg = f"checkpoint {args.checkpoint_id!r} not found"
        if args.json:
            print(json.dumps({"error": msg, "removed": False}, indent=2))
        else:
            print(msg, file=sys.stderr)
        return 1
    _save_course_manifest_update(args.course_id, data, manifest)
    payload = {
        "course_id": args.course_id,
        "removed_id": args.checkpoint_id,
        "count": len(manifest.get("checkpoints") or []),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    from . import ui
    remain = payload["count"]
    ui.emit_success(
        f"Removed checkpoint \"{args.checkpoint_id}\" "
        f"({remain} remaining)."
    )
    return 0


def _cmd_checkpoints_skip_defaults(args: argparse.Namespace) -> int:
    from .checkpoints import skip_defaults

    loaded = _load_course_for_checkpoint_op(args.course_id)
    if loaded is None:
        msg = f"course {args.course_id!r} not found"
        if args.json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            from . import ui
            ui.emit_error(msg)
        return 1
    _, data = loaded
    manifest = dict(data.get("manifest") or {})
    skip_defaults(manifest)
    _save_course_manifest_update(args.course_id, data, manifest)
    payload = {"course_id": args.course_id, "checkpoints": [], "count": 0}
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    from . import ui
    ui.emit_success(f"Checkpoints cleared for \"{args.course_id}\".")
    ui.emit_next_steps([
        f"axi classroom prep checkpoints add {args.course_id} <checkpoint_id>",
    ], title="Add custom milestones")
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    """``axi classroom demo`` — seed a running demo classroom.

    Ships skeptic-evaluation-in-60s per prd-classroom §2.5. The demo is
    fully populated: course manifest, corpus, system prompt, assessments,
    rails, roster, RAG policy.
    """
    from .demo import (
        DEMO_CLASSROOM_ID,
        DEMO_COURSE_ID,
        DEMO_TITLE,
        reset_demo,
        seed_demo,
    )

    if args.reset:
        reset_demo()
        action = "reset"
    else:
        seed_demo()
        action = "seeded"

    payload = {
        "action": action,
        "course_id": DEMO_COURSE_ID,
        "classroom_id": DEMO_CLASSROOM_ID,
        "title": DEMO_TITLE,
        "next_steps": [
            f"axi classroom prep status {DEMO_CLASSROOM_ID}",
            f"axi classroom prep dry-run {DEMO_CLASSROOM_ID}",
            "axi classroom prep from-demo <my-course-id> --instructor <you>",
        ],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    from . import ui
    ui.emit_success(f"Demo classroom {action}.")
    ui.emit_kv("Details", {
        "course": DEMO_COURSE_ID,
        "classroom": DEMO_CLASSROOM_ID,
        "title": DEMO_TITLE,
    })
    ui.emit_next_steps(payload["next_steps"])
    return 0


def _cmd_prep_tune_prompt(args: argparse.Namespace) -> int:
    """``axi classroom prep tune-prompt`` — one-shot prompt tune.

    Sets a system prompt on the course, tests it against a sample query,
    and persists. Mirrors the ``classroom_prep_tune_prompt`` chat tool so
    instructors see the same surface from CLI and chat.
    """
    from .chat_tools.prep_tools import _tool_tune_prompt

    result = _tool_tune_prompt(
        {
            "course_id": args.course_id,
            "system_prompt": args.system_prompt,
            "test_query": args.test_query,
        }
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if "error" not in result else 1
    from . import ui
    if "error" in result:
        ui.emit_error(result["error"])
        return 1
    ui.emit_success(f"Tuned prompt on \"{args.course_id}\".")
    ui.emit_kv("Test result", {
        "query": result.get("test_query", ""),
        "response": result.get("test_response", ""),
    })
    return 0


def _cmd_prep_from_demo(args: argparse.Namespace) -> int:
    """``axi classroom prep from-demo`` — clone the demo course + classroom."""
    from .demo import clone_demo

    new_classroom_id = args.new_classroom_id or None
    try:
        ids = clone_demo(
            new_course_id=args.new_course_id,
            instructor_id=args.instructor,
            new_classroom_id=new_classroom_id,
        )
    except ValueError as e:
        payload = {"error": str(e)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            from . import ui
            ui.emit_error(str(e))
        return 1

    course_id = ids["course_id"]
    classroom_id = ids["classroom_id"]
    payload = {
        "cloned_course_id": course_id,
        "cloned_classroom_id": classroom_id,
        "instructor_id": args.instructor,
        "next_steps": [
            f"axi classroom prep status {classroom_id}",
            f"axi classroom prep corpus {course_id}",
            f"axi classroom prep prompt {course_id}",
            f"axi classroom prep assessment {course_id}",
            f"axi classroom publish {classroom_id} --approver {args.instructor}",
        ],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    from . import ui
    ui.emit_success(
        f"Cloned demo into course \"{course_id}\" / classroom \"{classroom_id}\"."
    )
    ui.emit_info(f"Instructor: {args.instructor}")
    ui.emit_next_steps(
        payload["next_steps"],
        title="Edit what you'd like, then publish",
    )
    return 0


def _classroom_materials_store(classroom_id: str):
    """Return a ClassroomMaterialsStore rooted under the coordinator dir
    for ``classroom_id``. Honors $HOME for test isolation."""
    from .classroom_materials import ClassroomMaterialsStore

    base = (
        Path.home()
        / ".axi"
        / "coordinator"
        / "classrooms"
        / classroom_id
    )
    return ClassroomMaterialsStore(base)


def _build_seeded_fake_canvas():
    """Build a fake CanvasMockServer prepopulated with a small course
    (pages, announcements, files, module outline) so the demo path is
    visibly populated without live Canvas access."""
    from .lms.canvas_mock import CanvasMockServer

    mock = CanvasMockServer()
    mock.add_course("c1", "NE 101 — fake")
    mock.add_page("c1", url_slug="welcome", title="Welcome",
                  body="<h1>Welcome</h1><p>Course intro.</p>")
    mock.add_page("c1", url_slug="syllabus", title="Syllabus",
                  body="<h1>Syllabus</h1><p>Schedule.</p>")
    mock.add_announcement("c1", announcement_id="a1", title="Reading reminder",
                          message="<p>Reading by Friday.</p>",
                          posted_at="2026-01-15T10:00:00Z", author="Ondrej")
    mock.add_file("c1", file_id="f1", display_name="lecture-1-slides.pdf",
                  content_type="application/pdf", size=1024,
                  body=b"%PDF-1.4 stub")
    mock.add_module("c1", module_id="m1", name="Week 1 — fundamentals", position=1)
    mock.add_module_item(
        "c1", "m1", item_id="i1", type="Page",
        title="Welcome", content_id="welcome", position=1,
    )
    return mock


def _make_canvas_provider(args):
    """Construct a CanvasLMSProvider for the CLI args (fake or live)."""
    from .lms.canvas import CanvasLMSProvider

    if getattr(args, "fake", False):
        mock = _build_seeded_fake_canvas()
        return CanvasLMSProvider({
            "api_url": "mock://canvas",
            "api_token": "fake-token",
            "_mock_server": mock,
        })

    api_url = getattr(args, "canvas_url", "") or os.environ.get("CANVAS_URL", "")
    api_token = getattr(args, "canvas_token", "") or os.environ.get("CANVAS_TOKEN", "")
    if not api_token:
        return None
    return CanvasLMSProvider({"api_url": api_url, "api_token": api_token})


def _cmd_canvas_pull(args) -> int:
    """`axi classroom canvas pull <classroom_id> --canvas-course-id <id>`."""
    import json as _json

    from .canvas_pull import pull_course_to_materials

    provider = _make_canvas_provider(args)
    if provider is None:
        print("error: canvas-token (or CANVAS_TOKEN env) required for live pull",
              file=sys.stderr)
        return 2

    store = _classroom_materials_store(args.classroom_id)
    summary = pull_course_to_materials(provider, args.canvas_course_id, store)

    if args.json:
        print(_json.dumps(summary))
    else:
        print(
            f"Pulled {summary['total']} entries from Canvas course "
            f"{args.canvas_course_id}: "
            f"pages={summary['pages']}, announcements={summary['announcements']}, "
            f"files={summary['files']}, outline={summary['outline']}"
        )
    return 0


def _proposals_store(classroom_id: str):
    from .proposals import ProposalStore

    base = (
        Path.home()
        / ".axi"
        / "coordinator"
        / "classrooms"
        / classroom_id
        / "proposals"
    )
    return ProposalStore(base)


def _proposals_store_by_id(proposal_id: str):
    """Locate the ProposalStore that contains ``proposal_id`` by scanning
    every classroom under the coordinator root. Returns (store, classroom_id)
    or (None, None)."""
    coord_root = Path.home() / ".axi" / "coordinator" / "classrooms"
    if not coord_root.is_dir():
        return None, None
    for class_dir in coord_root.iterdir():
        candidate = class_dir / "proposals" / f"{proposal_id}.json"
        if candidate.exists():
            from .proposals import ProposalStore
            return ProposalStore(class_dir / "proposals"), class_dir.name
    return None, None


def _cmd_proposals_create(args) -> int:
    store = _proposals_store(args.classroom_id)
    proposal = store.create(
        classroom_id=args.classroom_id,
        target=args.target,
        target_id=args.target_id,
        action=args.action,
        title=args.title,
        body=args.body,
        created_by=args.created_by,
    )
    print(
        f"Created draft proposal {proposal.proposal_id} for {args.classroom_id} "
        f"({args.target} {args.action})."
    )
    return 0


def _cmd_proposals_list(args) -> int:
    import json as _json

    store = _proposals_store(args.classroom_id)
    rows = store.list(
        classroom_id=args.classroom_id,
        status=args.status or None,
    )
    if args.json:
        print(_json.dumps([p.to_dict() for p in rows]))
        return 0
    if not rows:
        print("(no proposals)")
        return 0
    for p in rows:
        print(
            f"  {p.proposal_id}  [{p.status}]  {p.target}/{p.action}  {p.title}"
        )
    return 0


def _cmd_proposals_approve(args) -> int:
    store, _classroom_id = _proposals_store_by_id(args.proposal_id)
    if store is None:
        print(f"error: no proposal found with id {args.proposal_id!r}",
              file=sys.stderr)
        return 1
    p = store.approve(args.proposal_id, approver=args.by)
    print(f"Approved proposal {p.proposal_id} (by {p.approved_by}).")
    return 0


def _cmd_proposals_reject(args) -> int:
    store, _classroom_id = _proposals_store_by_id(args.proposal_id)
    if store is None:
        print(f"error: no proposal found with id {args.proposal_id!r}",
              file=sys.stderr)
        return 1
    p = store.reject(args.proposal_id, reason=args.reason, rejecter=args.by)
    print(f"Rejected proposal {p.proposal_id} (reason: {p.rejected_reason}).")
    return 0


def _cmd_proposals_push(args) -> int:
    store, _classroom_id = _proposals_store_by_id(args.proposal_id)
    if store is None:
        print(f"error: no proposal found with id {args.proposal_id!r}",
              file=sys.stderr)
        return 1
    p = store.get(args.proposal_id)
    if p.status != "approved":
        print(
            f"error: proposal {p.proposal_id!r} is {p.status!r}; "
            "only approved proposals can be pushed",
            file=sys.stderr,
        )
        return 1

    provider = _make_canvas_provider(args)
    if provider is None:
        print("error: canvas-token (or CANVAS_TOKEN env) required for live push",
              file=sys.stderr)
        return 2

    course_id = args.canvas_course_id
    if p.target == "page" and p.action == "create":
        result = provider.create_page(course_id, title=p.title, body=p.body)
    elif p.target == "page" and p.action == "update":
        result = provider.update_page(
            course_id, url_slug=p.target_id, body=p.body, title=p.title
        )
    elif p.target == "announcement":
        result = provider.post_announcement(
            course_id, title=p.title, message=p.body
        )
    elif p.target == "assignment" and p.action == "update":
        result = provider.update_assignment_description(
            course_id, assignment_id=p.target_id, description=p.body
        )
    else:
        print(
            f"error: push not supported yet for target={p.target!r} action={p.action!r}",
            file=sys.stderr,
        )
        return 1

    if not result.success:
        print(f"error: LMS push failed: {result.message}", file=sys.stderr)
        return 1

    store.mark_pushed(p.proposal_id, lms_id=result.lms_id or result.url_slug)
    print(f"Pushed proposal {p.proposal_id} → LMS id {result.lms_id or result.url_slug}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        # Bare `axi classroom` — show role-aware orientation. Detect any
        # local classroom on this machine to tailor the suggested next
        # commands; fall back to "you're new here" otherwise.
        _emit_orientation()
        return 0
    return handler(args)


def _emit_orientation() -> None:
    """Friendly orientation for `axi classroom` with no subcommand.

    Three personas:
    - new user (no local state) — point at demo + help
    - instructor (coordinator dirs exist) — point at status + brief
    - student (memberships exist) — point at me + ask
    """
    from . import ui

    coord_root = Path.home() / ".axi" / "coordinator" / "classrooms"
    student_root = Path.home() / ".axi" / "classrooms"
    instructor_classes: list[str] = []
    student_classes: list[str] = []
    if coord_root.is_dir():
        instructor_classes = sorted(
            p.name for p in coord_root.iterdir() if p.is_dir()
        )
    if student_root.is_dir():
        student_classes = sorted(
            p.name for p in student_root.iterdir()
            if p.is_dir() and (p / "membership.json").is_file()
        )

    ui.out().print()
    if not instructor_classes and not student_classes:
        ui.out().print("[bold]Welcome to axi classroom.[/]")
        ui.out().print()
        ui.out().print("First time? Try the demo to see a working classroom in 60s:")
        ui.out().print("  [cyan]axi classroom demo[/]")
        ui.out().print()
        ui.out().print("Or jump straight in:")
        ui.out().print("  [cyan]axi classroom prep init --title \"...\" --instructor <you>[/]   (instructor)")
        ui.out().print("  [cyan]axi classroom join <invite-from-instructor>[/]                          (student)")
        ui.out().print()
        ui.out().print("[dim]Full command list: axi classroom --help[/]")
        return

    if instructor_classes:
        ui.out().print(f"[bold]You're hosting {len(instructor_classes)} class(es) on this machine:[/]")
        for cid in instructor_classes:
            ui.out().print(f"  • {cid}")
        ui.out().print()
        ui.out().print("Common next steps:")
        first = instructor_classes[0]
        ui.out().print("  [cyan]axi classroom status[/]                                   (one-line dashboard)")
        ui.out().print(f"  [cyan]axi classroom brief {first} --instructor <you>[/]")
        ui.out().print(f"  [cyan]axi classroom doctor {first}[/]                            (diagnose)")
        ui.out().print()

    if student_classes:
        ui.out().print(f"[bold]You're a student in {len(student_classes)} class(es):[/]")
        for cid in student_classes:
            ui.out().print(f"  • {cid}")
        ui.out().print()
        ui.out().print("Common next steps:")
        first = student_classes[0]
        ui.out().print(f"  [cyan]axi classroom ask {first} \"<question>\"[/]")
        ui.out().print(f"  [cyan]axi classroom me {first} --memory[/]                       (what's logged)")
        ui.out().print(f"  [cyan]axi classroom doctor {first}[/]                            (diagnose)")
        ui.out().print()

    ui.out().print("[dim]Full command list: axi classroom --help[/]")


if __name__ == "__main__":
    sys.exit(main())
