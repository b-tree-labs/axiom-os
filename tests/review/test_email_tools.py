# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.extensions.builtins.chat.tools_ext.email — email chat tools."""

from pathlib import Path

import pytest

from axiom.extensions.builtins.chat.tools_ext.email import execute


@pytest.fixture
def drafts_dir(tmp_path, monkeypatch):
    """Override the drafts directory to use tmp_path."""
    import axiom.extensions.builtins.chat.tools_ext.email as mod
    monkeypatch.setattr(mod, "_DRAFTS_DIR", tmp_path)
    return tmp_path


# ── email_draft ──────────────────────────────────────────────────────

class TestEmailDraft:
    def test_creates_draft_file(self, drafts_dir):
        result = execute("email_draft", {
            "to": "kevin@example.com",
            "subject": "Weekly Status",
            "body": "Here is the weekly status update.",
        })
        assert "error" not in result
        assert result["to"] == "kevin@example.com"
        assert result["subject"] == "Weekly Status"
        assert Path(result["path"]).exists()

    def test_draft_content_structure(self, drafts_dir):
        result = execute("email_draft", {
            "to": "kevin@example.com",
            "subject": "Weekly Status",
            "body": "Body content here.",
            "cc": "team@example.com",
        })
        path = Path(result["path"])
        content = path.read_text()
        assert "**To:** kevin@example.com" in content
        assert "**Subject:** Weekly Status" in content
        assert "**CC:** team@example.com" in content
        assert "---" in content
        assert "Body content here." in content

    def test_custom_filename(self, drafts_dir):
        result = execute("email_draft", {
            "to": "kevin@example.com",
            "subject": "Test",
            "body": "Content",
            "filename": "email_custom_name",
        })
        assert "custom_name" in result["filename"]

    def test_auto_filename_from_recipient(self, drafts_dir):
        result = execute("email_draft", {
            "to": "kevin.clarno@example.com",
            "subject": "Test",
            "body": "Content",
        })
        assert "kevin.clarno" in result["filename"]

    def test_missing_required_fields(self, drafts_dir):
        result = execute("email_draft", {
            "to": "kevin@example.com",
            "subject": "",
            "body": "Content",
        })
        assert "error" in result


# ── email_preview ────────────────────────────────────────────────────

class TestEmailPreview:
    def test_preview_existing_draft(self, drafts_dir):
        # Create a draft first
        execute("email_draft", {
            "to": "kevin@example.com",
            "subject": "Weekly Update",
            "body": "Here is the update.",
        })

        result = execute("email_preview", {})
        assert "error" not in result
        assert result["to"] == "kevin@example.com"
        assert result["subject"] == "Weekly Update"
        assert "update" in result["body"].lower()

    def test_preview_specific_file(self, drafts_dir):
        execute("email_draft", {
            "to": "alice@example.com",
            "subject": "First",
            "body": "First email",
            "filename": "email_first",
        })
        execute("email_draft", {
            "to": "bob@example.com",
            "subject": "Second",
            "body": "Second email",
            "filename": "email_second",
        })

        result = execute("email_preview", {"filename": "email_first"})
        assert result["to"] == "alice@example.com"

    def test_preview_not_found(self, drafts_dir):
        result = execute("email_preview", {"filename": "nonexistent"})
        assert "error" in result

    def test_preview_empty_dir(self, drafts_dir):
        result = execute("email_preview", {})
        assert "error" in result


# ── email_send ───────────────────────────────────────────────────────

class TestEmailSend:
    def test_send_requires_confirm(self, drafts_dir):
        execute("email_draft", {
            "to": "kevin@example.com",
            "subject": "Test",
            "body": "Content",
        })

        result = execute("email_send", {})
        assert "error" in result
        assert "confirm" in result["error"].lower()

    def test_send_no_drafts(self, drafts_dir):
        result = execute("email_send", {"confirm": True})
        assert "error" in result

    def test_send_smtp_not_configured(self, drafts_dir, monkeypatch):
        """Send should fail gracefully when SMTP is not configured."""
        execute("email_draft", {
            "to": "kevin@example.com",
            "subject": "Test",
            "body": "Content",
        })

        # Mock _load_smtp_config to return empty
        import axiom.extensions.builtins.chat.tools_ext.email as mod
        monkeypatch.setattr(mod, "_load_smtp_config", lambda: {})

        result = execute("email_send", {"confirm": True})
        assert "error" in result


# ── email_list ───────────────────────────────────────────────────────

class TestEmailList:
    def test_list_empty(self, drafts_dir):
        result = execute("email_list", {})
        assert result["drafts"] == []

    def test_list_with_drafts(self, drafts_dir):
        execute("email_draft", {
            "to": "a@example.com",
            "subject": "First",
            "body": "Body 1",
            "filename": "email_first_2026-03-04",
        })
        execute("email_draft", {
            "to": "b@example.com",
            "subject": "Second",
            "body": "Body 2",
            "filename": "email_second_2026-03-04",
        })

        result = execute("email_list", {})
        assert len(result["drafts"]) == 2
        filenames = [d["filename"] for d in result["drafts"]]
        assert "email_first_2026-03-04" in filenames
        assert "email_second_2026-03-04" in filenames

    def test_list_only_email_drafts(self, drafts_dir):
        """Non-email files in the drafts dir should not appear."""
        (drafts_dir / "program_review_2026-03-04.md").write_text("review content")
        execute("email_draft", {
            "to": "x@example.com",
            "subject": "Test",
            "body": "Body",
            "filename": "email_test",
        })

        result = execute("email_list", {})
        assert len(result["drafts"]) == 1


# ── unknown tool ─────────────────────────────────────────────────────

class TestUnknownEmailTool:
    def test_unknown_tool_name(self):
        result = execute("email_bogus", {})
        assert "error" in result
