# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Source code reaching the index at all, and reaching the right tier.

Before this, ``.py`` was not an ingestible extension and ``ingest_repo`` never
looked at ``src``. A corpus could hold every design document a project had
written and still not answer how any of it worked.
"""

from __future__ import annotations

from axiom.rag.extract import SUPPORTED_EXTENSIONS, extract_text
from axiom.rag.ingest import _TEXT_EXTENSIONS

MODULE = '''"""A builder."""

import os


def build(element):
    """Build it."""
    return element
'''


class TestPythonIsIngestibleAtAll:
    def test_the_extension_is_supported(self):
        assert ".py" in SUPPORTED_EXTENSIONS

    def test_it_is_read_as_text_not_through_an_extractor(self):
        """Already text. Running it through a document extractor would only be
        a chance to mangle it."""
        assert ".py" in _TEXT_EXTENSIONS

    def test_extract_text_returns_the_source_verbatim(self, tmp_path):
        path = tmp_path / "mod.py"
        path.write_text(MODULE)

        assert extract_text(path) == MODULE


class TestIngestReposLooksAtTheSource:
    def test_src_and_packages_are_searched(self):
        """The change that makes the rest matter: without these, fixing the
        extension reaches nothing."""
        import inspect

        from axiom.rag import ingest

        source = inspect.getsource(ingest.ingest_repo)
        assert 'repo_root / "src"' in source
        assert 'repo_root / "packages"' in source


class TestChunkingRoutesToTheSymbolChunker:
    def test_a_python_file_is_chunked_by_symbol(self, tmp_path, monkeypatch):
        """Not by the prose chunker, whatever tier was asked for."""
        from axiom.rag.python_chunker import chunk_python

        chunks = chunk_python(MODULE, "pkg/mod.py")

        assert [c.source_type for c in chunks] == ["python", "python"]
        assert "pkg.mod.build" in {c.source_title for c in chunks}

    def test_the_dispatch_is_in_ingest_file(self):
        import inspect

        from axiom.rag import ingest

        source = inspect.getsource(ingest.ingest_file)
        assert "chunk_python" in source
        assert 'suffix == ".py"' in source

    def test_an_unparseable_file_falls_back_rather_than_being_dropped(self):
        """Losing a repository's worth of context because one file is
        malformed is a bad trade."""
        import inspect

        from axiom.rag import ingest

        source = inspect.getsource(ingest.ingest_file)
        assert "if chunks is None:" in source


class TestExportControlStillGatesIt:
    """Source code is exactly what must not slip into a shared tier.

    Pulling external code into a community corpus without screening it would
    be the wrong kind of first, and code is likelier to carry a marker in a
    header comment than prose is.
    """

    def test_screening_runs_for_the_community_corpus(self):
        import inspect

        from axiom.rag import ingest

        source = inspect.getsource(ingest.ingest_file)
        assert 'corpus == "rag-community"' in source
        assert "screen_document" in source

    def test_a_marked_python_file_is_routed_away_from_community(self):
        from axiom.rag.ec_screening import screen_document

        marked = '# EXPORT CONTROLLED — ITAR\ndef build(x):\n    return x\n'
        result = screen_document("pkg/mod.py", marked, target_corpus="rag-community")

        assert not result.allowed_community, (
            "a marker in a source comment must block the community tier the "
            "same way one in prose does"
        )
        assert result.markers_found
