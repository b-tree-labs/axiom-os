# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ODT text comes from a parser that refuses external entities."""

from __future__ import annotations

import zipfile

import pytest

from axiom.rag.extract import _extract_odt

pytest.importorskip("defusedxml")


def _odt(path, content_xml: str):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("content.xml", content_xml)
    return path


def test_plain_odt_text_is_returned(tmp_path):
    path = _odt(
        tmp_path / "note.odt",
        """<?xml version="1.0"?>
        <office:document-content
            xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
            xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
          <office:body><office:text><text:p>hello odt</text:p></office:text></office:body>
        </office:document-content>
        """,
    )
    assert (_extract_odt(path) or "").strip() == "hello odt"


def test_an_external_entity_is_not_read(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("super-secret-token", encoding="utf-8")
    path = _odt(
        tmp_path / "evil.odt",
        f"""<?xml version="1.0"?>
        <!DOCTYPE foo [
          <!ENTITY xxe SYSTEM "file://{secret}">
        ]>
        <office:document-content
            xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
            xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
          <office:body><office:text><text:p>&xxe;</text:p></office:text></office:body>
        </office:document-content>
        """,
    )
    extracted = _extract_odt(path)
    assert extracted is None or "super-secret-token" not in extracted
