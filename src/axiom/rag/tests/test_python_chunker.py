# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chunking code at symbol boundaries, so a hit is something you can read.

Prose chunking splits on blank lines and a character budget. Applied to code it
yields "the last nine lines of one function and the first twenty of the next":
a body with no name, no signature and no docstring. These tests pin the
properties that make a retrieved chunk usable instead.
"""

from __future__ import annotations

from axiom.rag.python_chunker import (
    MAX_SYMBOL_CHARS,
    chunk_python,
    split_symbols,
)

SOURCE = '''"""What this module is for."""

import os
from pathlib import Path

CONSTANT = 3


def build(element, *, strict=False):
    """Build a thing from an element."""
    return {"built": element}


class Renderer:
    """Turns a spec into a deck."""

    def render(self, spec):
        """Render it."""
        return spec

    def validate(self, spec):
        return True
'''


def _titles(chunks):
    return [c.source_title for c in chunks]


class TestSymbolBoundaries:
    def test_each_top_level_symbol_is_its_own_chunk(self):
        titles = _titles(chunk_python(SOURCE, "pkg/mod.py"))

        assert "pkg.mod.build" in titles
        assert "pkg.mod.Renderer" in titles

    def test_a_function_arrives_whole(self):
        """Half a function is worth less than none: the reader cannot tell
        whether the part they got is the part that matters."""
        chunk = next(c for c in chunk_python(SOURCE, "pkg/mod.py") if c.source_title.endswith(".build"))

        assert "def build(element, *, strict=False):" in chunk.text
        assert '"""Build a thing from an element."""' in chunk.text
        assert 'return {"built": element}' in chunk.text

    def test_the_line_number_points_at_the_definition(self):
        """So a citation lands on the def, not on the top of the file."""
        chunk = next(c for c in chunk_python(SOURCE, "pkg/mod.py") if c.source_title.endswith(".build"))

        assert SOURCE.splitlines()[chunk.start_line - 1].startswith("def build")


class TestEveryChunkSaysWhatItIs:
    def test_the_header_names_the_file_symbol_and_line(self):
        """Retrieval returns text. A body with no name cannot be checked."""
        chunk = next(c for c in chunk_python(SOURCE, "pkg/mod.py") if c.source_title.endswith(".build"))
        header = chunk.text.splitlines()[0]

        assert "pkg/mod.py" in header
        assert "pkg.mod.build" in header
        assert str(chunk.start_line) in header

    def test_chunks_are_marked_as_python(self):
        assert all(c.source_type == "python" for c in chunk_python(SOURCE, "pkg/mod.py"))


class TestTheModuleOverview:
    def test_it_answers_what_is_this_file_for(self):
        overview = chunk_python(SOURCE, "pkg/mod.py")[0]

        assert overview.source_title == "pkg.mod"
        assert "What this module is for." in overview.text

    def test_it_answers_what_does_it_depend_on(self):
        """The part a symbol-only chunker throws away."""
        overview = chunk_python(SOURCE, "pkg/mod.py")[0]

        assert "import os" in overview.text
        assert "from pathlib import Path" in overview.text

    def test_it_lists_what_the_module_defines(self):
        overview = chunk_python(SOURCE, "pkg/mod.py")[0]

        assert "def build" in overview.text
        assert "class Renderer" in overview.text


class TestModuleNaming:
    def test_a_src_prefix_is_dropped(self):
        """``src`` is a source root, not a package — the same rule the port
        needed for langgraph.json entries."""
        assert chunk_python(SOURCE, "src/pkg/mod.py")[0].source_title == "pkg.mod"

    def test_a_package_init_names_the_package(self):
        assert chunk_python(SOURCE, "pkg/sub/__init__.py")[0].source_title == "pkg.sub"

    def test_windows_separators(self):
        assert chunk_python(SOURCE, "pkg\\\\mod.py")[0].source_title == "pkg.mod"


class TestALongClassIsSplitPerMethod:
    def _long_class(self):
        methods = "\n".join(
            f"    def method_{i}(self):\n        return {i} * {'x' * 60}\n"
            for i in range(40)
        )
        return f'class Big:\n    """A big one."""\n\n{methods}'

    def test_it_splits(self):
        """Kept whole it exceeds any budget and then matches every query about
        any of its methods."""
        source = self._long_class()
        assert len(source) > MAX_SYMBOL_CHARS

        titles = _titles(chunk_python(source, "pkg/mod.py"))
        assert "pkg.mod.Big.method_0" in titles
        assert "pkg.mod.Big.method_39" in titles

    def test_each_method_keeps_its_class_for_context(self):
        """A method read without its class is a fragment."""
        chunks = chunk_python(self._long_class(), "pkg/mod.py")
        method = next(c for c in chunks if c.source_title.endswith(".method_7"))

        assert "class Big:" in method.text
        assert "A big one." in method.text
        assert "def method_7" in method.text

    def test_a_short_class_is_kept_whole(self):
        """Splitting a small class would scatter something readable."""
        chunks = chunk_python(SOURCE, "pkg/mod.py")

        renderer = next(c for c in chunks if c.source_title == "pkg.mod.Renderer")
        assert "def render" in renderer.text
        assert "def validate" in renderer.text
        assert not any(c.source_title.endswith("Renderer.render") for c in chunks)


class TestAFileThatWillNotParse:
    def test_it_returns_none_rather_than_raising(self):
        """A template or a Python 2 leftover must not cost the whole repo."""
        assert chunk_python("def broken(:\n", "pkg/bad.py") is None
        assert split_symbols("def broken(:\n", "pkg/bad.py") is None

    def test_an_empty_file_yields_nothing(self):
        """Not a crash, and not a row either.

        An empty ``__init__.py`` is the common case and there are thousands of
        them. Indexing each as "Module pkg.sub" adds rows that can never answer
        anything and dilute every neighbouring match.
        """
        assert chunk_python("", "pkg/empty.py") == []

    def test_a_package_init_with_only_imports_is_still_indexed(self):
        """Re-exports are how a package says what it offers."""
        source = "from .mod import build, Renderer\n"
        chunks = chunk_python(source, "pkg/__init__.py")

        assert len(chunks) == 1
        assert "from .mod import build, Renderer" in chunks[0].text


class TestAsyncAndDecorated:
    def test_an_async_function_is_a_symbol(self):
        source = "async def fetch(url):\n    return url\n"
        assert "pkg.mod.fetch" in _titles(chunk_python(source, "pkg/mod.py"))

    def test_a_decorated_function_keeps_its_body(self):
        source = "import functools\n\n@functools.cache\ndef memo(x):\n    return x\n"
        chunk = next(c for c in chunk_python(source, "pkg/mod.py") if c.source_title.endswith(".memo"))
        assert "def memo(x):" in chunk.text
