# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""``BoxIngestSource(exclude_prefixes=...)`` never walks an excluded subtree.

Why: a connector's Box root can hold subtrees that are not documents at all.
On the ut-triga node the DMSR root's ``TRIGA_realtime_logging`` folders held
413 GB of telemetry CSVs and plot HTML; walking them would have filled the
disk and put none of it to any use in a RAG corpus.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.sources.box.source import (
    BoxIngestSource,
    exclude_prefixes_from_params,
    normalize_exclude_prefixes,
)


class _Api:
    def __init__(self, tree):
        self.tree = tree
        self.listed: list[str] = []

    def get_json(self, path, params=None):
        assert path.startswith("/folders/") and path.endswith("/items")
        fid = path.split("/")[2]
        self.listed.append(fid)
        return {"entries": self.tree.get(fid, []), "next_marker": None}

    def get_bytes(self, path):
        return b"x"


def _entry(kind, id, name, parents):
    e = {
        "type": kind,
        "id": str(id),
        "name": name,
        "etag": "0",
        "modified_at": "2026-06-01T00:00:00Z",
        "size": 1,
        "path_collection": {"entries": [{"name": "All Files"}] + [{"name": p} for p in parents]},
    }
    return e


TREE = {
    "root": [
        _entry("folder", "docs", "Docs", ["Corpus"]),
        _entry("folder", "tele", "TRIGA_realtime_logging", ["Corpus"]),
        _entry("folder", "tele2", "TRIGA_realtime_logging_notes", ["Corpus"]),
        _entry("file", 1, "top.pdf", ["Corpus"]),
    ],
    "docs": [_entry("file", 2, "paper.pdf", ["Corpus", "Docs"])],
    "tele": [_entry("file", 3, "serial.csv", ["Corpus", "TRIGA_realtime_logging"])],
    "tele2": [_entry("file", 4, "notes.md", ["Corpus", "TRIGA_realtime_logging_notes"])],
}


def test_an_excluded_subtree_contributes_no_items_and_is_never_listed():
    api = _Api(TREE)
    src = BoxIngestSource(
        name="t",
        folder_id="root",
        api_client=api,
        exclude_prefixes=["/Corpus/TRIGA_realtime_logging"],
    )

    ids = sorted(int(i.item_id) for i in src.catalog())

    assert ids == [1, 2, 4]
    assert "tele" not in api.listed, "an excluded folder must not cost an API call"


def test_exclusion_matches_whole_segments_only():
    src = BoxIngestSource(
        name="t",
        folder_id="root",
        api_client=_Api(TREE),
        exclude_prefixes=["/Corpus/TRIGA_realtime_logging"],
    )
    assert src._excluded("/Corpus/TRIGA_realtime_logging/x.csv")
    assert src._excluded("/Corpus/TRIGA_realtime_logging")
    assert not src._excluded("/Corpus/TRIGA_realtime_logging_notes/n.md")
    assert not src._excluded(None)


def test_no_exclusions_walks_everything():
    ids = sorted(
        int(i.item_id)
        for i in BoxIngestSource(name="t", folder_id="root", api_client=_Api(TREE)).catalog()
    )
    assert ids == [1, 2, 3, 4]


def test_prefixes_are_normalised_from_connector_params():
    assert normalize_exclude_prefixes(
        [" Corpus/TRIGA_realtime_logging/ ", "", "/Corpus/TRIGA_realtime_logging", "/"]
    ) == [
        "/Corpus/TRIGA_realtime_logging",
    ]
    assert exclude_prefixes_from_params({"exclude_prefixes": "/A/B, C/D ,,"}) == ["/A/B", "/C/D"]
    assert exclude_prefixes_from_params({}) == []
    assert exclude_prefixes_from_params(None) == []


def test_register_args_carry_exclusions_into_connector_params():
    import argparse

    from axiom.extensions.builtins.data_platform.sources.box.provider import BoxSourceProvider

    prov = BoxSourceProvider()
    parser = argparse.ArgumentParser()
    prov.add_register_args(parser)
    ns = parser.parse_args(
        [
            "--folder-id",
            "42",
            "--exclude-prefix",
            "Corpus/TRIGA_realtime_logging/",
            "--exclude-prefix",
            "/Corpus/Archive",
        ]
    )

    params = prov.params_from_args(ns)

    assert params["exclude_prefixes"] == "/Corpus/TRIGA_realtime_logging,/Corpus/Archive"
    assert exclude_prefixes_from_params(params) == [
        "/Corpus/TRIGA_realtime_logging",
        "/Corpus/Archive",
    ]


def test_no_exclude_args_leaves_params_untouched():
    import argparse

    from axiom.extensions.builtins.data_platform.sources.box.provider import BoxSourceProvider

    prov = BoxSourceProvider()
    parser = argparse.ArgumentParser()
    prov.add_register_args(parser)
    params = prov.params_from_args(parser.parse_args(["--folder-id", "42"]))
    assert "exclude_prefixes" not in params
