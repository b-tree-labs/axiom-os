# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""``BoxIngestSource`` skips excluded extensions and over-cap files at catalog time.

Why: on the ut-triga node a full pass with the telemetry subtree already
excluded still set out to fetch 268 GB into 141 GB of free disk; 12 GB of
photographs landed before it was killed. Media and multi-gigabyte outputs on
a shared corpus root are not documents, and the decision must be made from the
listing, before a single byte is fetched.
"""
from __future__ import annotations

import argparse

import pytest

from axiom.extensions.builtins.data_platform.sources.box.provider import BoxSourceProvider
from axiom.extensions.builtins.data_platform.sources.box.source import (
    BoxIngestSource,
    exclude_extensions_from_params,
    max_item_bytes_from_params,
    normalize_extensions,
)


class _Api:
    def __init__(self, tree):
        self.tree = tree
        self.fetched: list[str] = []

    def get_json(self, path, params=None):
        assert path.startswith("/folders/") and path.endswith("/items")
        return {"entries": self.tree.get(path.split("/")[2], []), "next_marker": None}

    def get_bytes(self, path):
        self.fetched.append(path)
        return b"x"


def _f(id, name, size):
    return {"type": "file", "id": str(id), "name": name, "etag": "0", "modified_at": "2026-06-01T00:00:00Z",
            "size": size, "path_collection": {"entries": [{"name": "All Files"}, {"name": "Corpus"}]}}


TREE = {"root": [
    _f(1, "paper.pdf", 2_000_000),
    _f(2, "photo.JPG", 5_000_000),
    _f(3, "clip.mp4", 900_000_000),
    _f(4, "huge-output.h5", 3_000_000_000),
    _f(5, "notes.md", 10_000),
    {"type": "file", "id": "6", "name": "nosize.txt", "etag": "0", "modified_at": "2026-06-01T00:00:00Z",
     "path_collection": {"entries": [{"name": "All Files"}, {"name": "Corpus"}]}},
]}


def _ids(src):
    return sorted(int(i.item_id) for i in src.catalog())


def test_excluded_extensions_are_never_cataloged_case_insensitively():
    src = BoxIngestSource(name="t", folder_id="root", api_client=_Api(TREE), exclude_extensions=["jpg", ".MP4"])
    assert _ids(src) == [1, 4, 5, 6]


def test_files_over_the_byte_cap_are_never_cataloged_and_unknown_size_is_kept():
    src = BoxIngestSource(name="t", folder_id="root", api_client=_Api(TREE), max_item_bytes=100_000_000)
    assert _ids(src) == [1, 2, 5, 6]


def test_no_policy_catalogs_everything():
    assert _ids(BoxIngestSource(name="t", folder_id="root", api_client=_Api(TREE))) == [1, 2, 3, 4, 5, 6]


def test_a_cap_below_one_byte_is_rejected():
    with pytest.raises(ValueError):
        BoxIngestSource(name="t", folder_id="root", api_client=_Api(TREE), max_item_bytes=0)


def test_params_round_trip_through_the_provider():
    prov = BoxSourceProvider()
    parser = argparse.ArgumentParser()
    prov.add_register_args(parser)
    ns = parser.parse_args(["--folder-id", "42", "--exclude-ext", ".JPG", "--exclude-ext", "mp4", "--max-item-mb", "200"])

    params = prov.params_from_args(ns)

    assert params["exclude_extensions"] == "jpg,mp4"
    assert params["max_item_bytes"] == str(200 * 1024 * 1024)
    assert exclude_extensions_from_params(params) == ["jpg", "mp4"]
    assert max_item_bytes_from_params(params) == 200 * 1024 * 1024
    assert max_item_bytes_from_params({}) is None
    assert normalize_extensions([" .Png ", "", "png"]) == ["png"]


def test_no_policy_args_leave_params_untouched():
    prov = BoxSourceProvider()
    parser = argparse.ArgumentParser()
    prov.add_register_args(parser)
    params = prov.params_from_args(parser.parse_args(["--folder-id", "42"]))
    assert "exclude_extensions" not in params and "max_item_bytes" not in params
