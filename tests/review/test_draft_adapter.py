# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.review.adapters.draft_adapter — section parser, draft discovery, approved output."""


from axiom.review.adapters.draft_adapter import (
    DraftReviewAdapter,
    create_draft_session,
    find_draft,
    parse_draft_sections,
    write_approved_draft,
)
from axiom.review.models import (
    ReviewDecision,
    ReviewItem,
    ReviewSession,
    ReviewSessionStore,
)

# ── sample content ───────────────────────────────────────────────────

SAMPLE_DRAFT = """\
# Weekly Summary — 2026-03-04

Week of 2026-03-04: 10 signals across 3 initiatives.

## Project Beta

### Progress

- Published 8 documents to publisher.

### Action Items

- Compile responses into calculator inputs.

## Project Alpha

### Progress

- Discovered 5 new repositories.

### Blockers

- GitLab export missing child repos.

## Uncategorized

### Progress

- 55 commits total.

*Generated: 2026-03-04T01:10:21Z*
"""

MINIMAL_DRAFT = """\
# Title

Just a preamble, no sections.
"""


# ── parse_draft_sections ─────────────────────────────────────────────

class TestParseDraftSections:
    def test_splits_at_h2(self):
        items = parse_draft_sections(SAMPLE_DRAFT)
        # Preamble + 3 sections
        assert len(items) == 4

    def test_preamble_heading(self):
        items = parse_draft_sections(SAMPLE_DRAFT)
        assert items[0].heading == "(Preamble)"
        assert "Weekly Summary" in items[0].content

    def test_section_headings(self):
        items = parse_draft_sections(SAMPLE_DRAFT)
        headings = [i.heading for i in items]
        assert "## Project Beta" in headings
        assert "## Project Alpha" in headings
        assert "## Uncategorized" in headings

    def test_section_content(self):
        items = parse_draft_sections(SAMPLE_DRAFT)
        beta_item = [i for i in items if "Project Beta" in i.heading][0]
        assert "Published 8 documents" in beta_item.content
        assert "Compile responses" in beta_item.content

    def test_item_ids_unique(self):
        items = parse_draft_sections(SAMPLE_DRAFT)
        ids = [i.item_id for i in items]
        assert len(ids) == len(set(ids))

    def test_minimal_preamble_only(self):
        items = parse_draft_sections(MINIMAL_DRAFT)
        assert len(items) == 1
        assert items[0].heading == "(Preamble)"

    def test_empty_content(self):
        items = parse_draft_sections("")
        assert len(items) == 0

    def test_all_items_start_pending(self):
        items = parse_draft_sections(SAMPLE_DRAFT)
        assert all(i.status == "pending" for i in items)


# ── find_draft ───────────────────────────────────────────────────────

class TestFindDraft:
    def test_explicit_path(self, tmp_path):
        draft = tmp_path / "my_draft.md"
        draft.write_text("content")
        assert find_draft(file_arg=str(draft)) == draft

    def test_explicit_path_not_found(self, tmp_path):
        assert find_draft(
            drafts_dir=tmp_path,
            file_arg="nonexistent.md",
        ) is None

    def test_auto_picks_most_recent(self, tmp_path):
        import time
        old = tmp_path / "weekly_summary_2026-02-25.md"
        old.write_text("old")
        time.sleep(0.05)
        new = tmp_path / "weekly_summary_2026-03-04.md"
        new.write_text("new")

        result = find_draft(drafts_dir=tmp_path)
        assert result == new

    def test_finds_weekly_program_review(self, tmp_path):
        draft = tmp_path / "weekly_program_review_2026-03-04.md"
        draft.write_text("review content")
        assert find_draft(drafts_dir=tmp_path) == draft

    def test_finds_program_review(self, tmp_path):
        draft = tmp_path / "program_review_week_ending_2026-03-04.md"
        draft.write_text("program review content")
        assert find_draft(drafts_dir=tmp_path) == draft

    def test_excludes_changelogs(self, tmp_path):
        """Changelogs (table format) should not be picked up."""
        import time
        draft = tmp_path / "weekly_summary_2026-03-04.md"
        draft.write_text("prose content")
        time.sleep(0.05)
        changelog = tmp_path / "changelog_2026-03-04.md"
        changelog.write_text("table content")
        # Should pick the weekly summary, not the newer changelog
        result = find_draft(drafts_dir=tmp_path)
        assert result == draft

    def test_empty_dir(self, tmp_path):
        assert find_draft(drafts_dir=tmp_path) is None

    def test_relative_to_drafts_dir(self, tmp_path):
        draft = tmp_path / "weekly_summary_2026-03-04.md"
        draft.write_text("content")
        result = find_draft(drafts_dir=tmp_path, file_arg="weekly_summary_2026-03-04.md")
        assert result == draft


# ── write_approved_draft ─────────────────────────────────────────────

class TestWriteApprovedDraft:
    def test_writes_accepted_sections(self, tmp_path):
        session = ReviewSession(
            session_id="test",
            session_type="draft",
            source="/path/to/weekly_summary_2026-03-04.md",
            source_hash="abc",
            started_at="2026-03-04T09:00:00Z",
            items=[
                ReviewItem(item_id="1", heading="(Preamble)", content="# Title", status="accepted"),
                ReviewItem(item_id="2", heading="## Good", content="## Good\n\nKeep this.", status="accepted"),
                ReviewItem(item_id="3", heading="## Bad", content="## Bad\n\nDrop this.", status="rejected"),
            ],
        )

        approved_dir = tmp_path / "approved"
        out = write_approved_draft(session, approved_dir=approved_dir)

        assert out is not None
        assert out.exists()
        content = out.read_text()
        assert "# Title" in content
        assert "Keep this" in content
        assert "Drop this" not in content

    def test_uses_edited_content(self, tmp_path):
        session = ReviewSession(
            session_id="test",
            session_type="draft",
            source="/path/to/weekly_summary_2026-03-04.md",
            source_hash="abc",
            started_at="2026-03-04T09:00:00Z",
            items=[
                ReviewItem(
                    item_id="1",
                    heading="## Section",
                    content="Original",
                    status="edited",
                    decisions=[
                        ReviewDecision(reviewer="x", status="edited", edited_content="Revised version"),
                    ],
                ),
            ],
        )

        approved_dir = tmp_path / "approved"
        out = write_approved_draft(session, approved_dir=approved_dir)
        assert out is not None
        content = out.read_text()
        assert "Revised version" in content
        assert "Original" not in content

    def test_all_rejected_returns_none(self, tmp_path):
        session = ReviewSession(
            session_id="test",
            session_type="draft",
            source="/path/to/file.md",
            source_hash="abc",
            started_at="2026-03-04T09:00:00Z",
            items=[
                ReviewItem(item_id="1", heading="## Only", content="Drop me", status="rejected"),
            ],
        )
        assert write_approved_draft(session, approved_dir=tmp_path / "approved") is None

    def test_output_filename_from_source(self, tmp_path):
        session = ReviewSession(
            session_id="test",
            session_type="draft",
            source="/drafts/weekly_summary_2026-03-04.md",
            source_hash="abc",
            started_at="2026-03-04T09:00:00Z",
            items=[
                ReviewItem(item_id="1", heading="## A", content="Keep", status="accepted"),
            ],
        )
        approved_dir = tmp_path / "approved"
        out = write_approved_draft(session, approved_dir=approved_dir)
        assert out.name == "weekly_summary_2026-03-04.md"

    def test_skipped_items_included(self, tmp_path):
        """Skipped/pending items are NOT dropped — only rejected ones are."""
        session = ReviewSession(
            session_id="test",
            session_type="draft",
            source="/path/to/file.md",
            source_hash="abc",
            started_at="2026-03-04T09:00:00Z",
            items=[
                ReviewItem(item_id="1", heading="## Kept", content="I'm skipped", status="skipped"),
                ReviewItem(item_id="2", heading="## Also", content="I'm pending", status="pending"),
            ],
        )
        approved_dir = tmp_path / "approved"
        out = write_approved_draft(session, approved_dir=approved_dir)
        content = out.read_text()
        assert "I'm skipped" in content
        assert "I'm pending" in content


# ── create_draft_session ─────────────────────────────────────────────

class TestCreateDraftSession:
    def test_creates_new_session(self, tmp_path):
        draft = tmp_path / "weekly_summary_2026-03-04.md"
        draft.write_text(SAMPLE_DRAFT)
        store = ReviewSessionStore(state_path=tmp_path / "state.json")

        session = create_draft_session(draft, store)
        assert session.session_type == "draft"
        assert len(session.items) == 4  # preamble + 3 sections
        assert session.source == str(draft)

    def test_resumes_existing_session(self, tmp_path):
        draft = tmp_path / "weekly_summary_2026-03-04.md"
        draft.write_text(SAMPLE_DRAFT)
        store = ReviewSessionStore(state_path=tmp_path / "state.json")

        session1 = create_draft_session(draft, store)
        session1.items[0].status = "accepted"
        store.save(session1)

        session2 = create_draft_session(draft, store)
        assert session2.session_id == session1.session_id
        assert session2.items[0].status == "accepted"

    def test_fresh_flag_ignores_existing(self, tmp_path):
        draft = tmp_path / "weekly_summary_2026-03-04.md"
        draft.write_text(SAMPLE_DRAFT)
        store = ReviewSessionStore(state_path=tmp_path / "state.json")

        session1 = create_draft_session(draft, store)
        session2 = create_draft_session(draft, store, fresh=True)
        assert session2.session_id != session1.session_id

    def test_source_change_restarts(self, tmp_path):
        draft = tmp_path / "weekly_summary_2026-03-04.md"
        draft.write_text(SAMPLE_DRAFT)
        store = ReviewSessionStore(state_path=tmp_path / "state.json")

        session1 = create_draft_session(draft, store)
        original_id = session1.session_id

        # Modify the draft
        draft.write_text(SAMPLE_DRAFT + "\n## New Section\n\nNew content.\n")
        session2 = create_draft_session(draft, store)
        assert session2.session_id != original_id


# ── DraftReviewAdapter ───────────────────────────────────────────────

class TestDraftReviewAdapter:
    def test_get_commands(self):
        adapter = DraftReviewAdapter()
        cmds = adapter.get_commands()
        assert "E" in cmds
        assert "D" in cmds
        assert "C" in cmds

    def test_handle_drop(self):
        adapter = DraftReviewAdapter()
        item = ReviewItem(item_id="1", heading="## X", content="content")
        result = adapter.handle_command("D", item)
        assert result == "rejected"

    def test_handle_unknown_command(self):
        adapter = DraftReviewAdapter()
        item = ReviewItem(item_id="1", heading="## X", content="content")
        result = adapter.handle_command("Z", item)
        assert result is None
