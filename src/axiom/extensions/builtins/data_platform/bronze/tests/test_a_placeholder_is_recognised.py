# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A placeholder is not a document (#1948).

A source can hold a stand-in where the file should be: a Git LFS pointer
(a small text file naming an object that was never fetched), or bytes that
are not the format the name promises. Landing one as a document means
extraction fails quietly and the file is missing from retrieval with no one
told. The detector is a pure function over the bytes and the name.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.bronze.placeholder import (
    LFS_POINTER,
    detect_placeholder,
)

LFS = (
    b"version https://git-lfs.github.com/spec/v1\n"
    b"oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
    b"size 12345\n"
)


def test_a_git_lfs_pointer_is_a_placeholder_whatever_its_name():
    assert detect_placeholder(LFS, "papers/report.pdf") == LFS_POINTER
    assert detect_placeholder(LFS, "notes/readme.md") == LFS_POINTER
    assert detect_placeholder(LFS, None) == LFS_POINTER
    assert LFS_POINTER == "placeholder: git-lfs pointer"


def test_a_large_file_that_mentions_the_lfs_spec_is_not_a_pointer():
    text = LFS + b"\n" + b"prose about LFS pointers. " * 200
    assert detect_placeholder(text, "notes/lfs.md") is None


@pytest.mark.parametrize(
    "name, head",
    [
        ("papers/report.pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"),
        ("papers/report.PDF", b"\n\n%PDF-1.4\n"),
        ("images/figure.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"),
        ("images/photo.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF"),
        ("images/photo.jpeg", b"\xff\xd8\xff\xe1"),
        ("docs/memo.docx", b"PK\x03\x04\x14\x00\x06\x00"),
        ("docs/table.xlsx", b"PK\x03\x04\x14\x00\x06\x00"),
        ("docs/deck.pptx", b"PK\x03\x04\x14\x00\x06\x00"),
        ("notes/readme.md", b"# Title\n"),
        ("data/unknown.bin", b"\x00\x01\x02"),
    ],
)
def test_real_files_are_not_placeholders(name, head):
    assert detect_placeholder(head + b"\x00" * 64, name) is None


@pytest.mark.parametrize(
    "name, head, ext",
    [
        ("papers/report.pdf", b"<html><body>Sign in</body></html>", ".pdf"),
        ("images/figure.png", b"GIF89a", ".png"),
        ("images/photo.jpg", b"not an image", ".jpg"),
        ("docs/memo.docx", b"plain text where a document should be", ".docx"),
    ],
)
def test_content_that_contradicts_its_extension_is_a_placeholder(name, head, ext):
    assert detect_placeholder(head, name) == f"placeholder: {ext} without its signature"


def test_empty_content_is_left_to_the_empty_content_path():
    assert detect_placeholder(b"", "papers/report.pdf") is None


def test_the_size_can_be_given_when_only_the_head_was_read():
    # The embedder's defensive check reads only the head of a blob.
    assert detect_placeholder(LFS, "papers/report.pdf", size=len(LFS)) == LFS_POINTER
    assert detect_placeholder(LFS, "notes/lfs.md", size=10_000_000) is None
