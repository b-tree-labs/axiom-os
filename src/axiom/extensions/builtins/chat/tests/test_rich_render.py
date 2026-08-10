# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for rich render provider and markdown utilities."""

import pytest

from axiom.extensions.builtins.chat.providers.markdown_utils import (
    extract_code_blocks,
    is_diff,
    terminal_width,
    truncate_result,
)


class TestMarkdownUtils:
    """Test shared markdown utilities."""

    def test_terminal_width_capped(self):
        width = terminal_width(cap=120)
        assert 1 <= width <= 120

    def test_is_diff_positive(self):
        text = """--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
-old line
+new line
 unchanged"""
        assert is_diff(text) is True

    def test_is_diff_negative(self):
        text = "This is just regular text\nwith multiple lines\nnothing special."
        assert is_diff(text) is False

    def test_is_diff_short_text(self):
        assert is_diff("a\nb") is False

    def test_extract_code_blocks(self):
        text = """Some text
```python
def hello():
    print("hi")
```
More text
```
plain code
```"""
        blocks = extract_code_blocks(text)
        assert len(blocks) == 2
        assert blocks[0][0] == "python"
        assert "def hello" in blocks[0][1]
        assert blocks[1][0] == ""
        assert "plain code" in blocks[1][1]

    def test_extract_no_code_blocks(self):
        blocks = extract_code_blocks("No code blocks here.")
        assert blocks == []

    def test_truncate_short(self):
        text = "Line 1\nLine 2\nLine 3"
        assert truncate_result(text, max_lines=5) == text

    def test_truncate_long(self):
        text = "\n".join(f"Line {i}" for i in range(20))
        result = truncate_result(text, max_lines=5)
        lines = result.splitlines()
        assert len(lines) == 5
        assert "more lines" in lines[-1]


class TestRichRenderProvider:
    """Test RichRenderProvider if rich is available."""

    @pytest.fixture
    def rich_provider(self):
        try:
            from axiom.extensions.builtins.chat.providers.rich_render import (
                RichRenderProvider,
            )

            return RichRenderProvider()
        except ImportError:
            pytest.skip("rich not installed")
            return None

    def test_is_render_provider(self, rich_provider):
        from axiom.extensions.builtins.chat.providers.base import RenderProvider

        assert isinstance(rich_provider, RenderProvider)

    def test_stream_text(self, rich_provider):
        from axiom.infra.gateway import StreamChunk

        chunks = iter(
            [
                StreamChunk(type="text", text="Hello world!"),
                StreamChunk(type="done"),
            ]
        )
        result = rich_provider.stream_text(chunks)
        assert "Hello world!" in result

    def test_render_status(self, rich_provider):
        # Just verify it doesn't crash
        rich_provider.render_status("claude-3-sonnet", 500, 200, 0.003)

    def test_render_thinking(self, rich_provider):
        text = "\n".join(f"Thought {i}" for i in range(10))
        rich_provider.render_thinking(text, collapsed=True)

    def test_render_thinking_empty(self, rich_provider):
        rich_provider.render_thinking("")

    def test_render_message(self, rich_provider):
        rich_provider.render_message("assistant", "# Hello\n\nThis is **bold** text.")

    def test_render_session_list_empty(self, rich_provider):
        rich_provider.render_session_list([])

    def test_render_tool_result(self, rich_provider):
        rich_provider.render_tool_result("query_docs", {"documents": []}, 0.3)

    def test_render_action_result(self, rich_provider):
        from axiom.infra.orchestrator.actions import Action

        action = Action(name="query_docs", params={})
        action.complete({"documents": []})
        rich_provider.render_action_result(action)
