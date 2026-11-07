# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The startup index cache must be fast when valid and invisible when not.

The properties that matter: a change to the inputs is never served stale, a
missing or damaged cache is a miss rather than an error, writes are atomic,
and a scan that was cut short is never remembered.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions import discovery, index_cache


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AXIOM_INDEX_CACHE", raising=False)
    yield


def _write_ext(root, name: str, noun: str, description: str = "d") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "axiom-extension.toml").write_text(
        f'[extension]\nname = "{name}"\nversion = "0.1.0"\ndescription = "x"\n\n'
        f'[[extension.provides]]\nkind = "cmd"\nnoun = "{noun}"\nentry = "{name}.cli:main"\n'
        f'description = "{description}"\n',
        encoding="utf-8",
    )


class TestStore:
    def test_roundtrip(self):
        key = index_cache.fingerprint("k", ["a"])
        assert index_cache.load("k", key) is None
        index_cache.store("k", key, {"x": [1, 2]})
        assert index_cache.load("k", key) == {"x": [1, 2]}

    def test_a_different_input_is_a_miss(self):
        index_cache.store("k", index_cache.fingerprint("k", ["a"]), 1)
        assert index_cache.load("k", index_cache.fingerprint("k", ["b"])) is None

    def test_a_different_axiom_version_is_a_miss(self, monkeypatch):
        monkeypatch.setattr(index_cache, "_axiom_version", lambda: "1.0")
        key_old = index_cache.fingerprint("k", ["a"])
        index_cache.store("k", key_old, 1)
        monkeypatch.setattr(index_cache, "_axiom_version", lambda: "2.0")
        assert index_cache.load("k", index_cache.fingerprint("k", ["a"])) is None

    @pytest.mark.parametrize("junk", ["", "{", "[]", '{"schema": 999}', "\x00\x01"])
    def test_a_damaged_file_is_a_miss_not_an_error(self, junk):
        key = index_cache.fingerprint("k", [])
        index_cache.store("k", key, 1)
        path = index_cache._path_for("k", key)
        path.write_text(junk, encoding="utf-8")
        assert index_cache.load("k", key) is None

    def test_absent_directory_is_a_miss(self):
        assert index_cache.load("k", index_cache.fingerprint("k", [])) is None

    def test_disabled_reads_and_writes_nothing(self, monkeypatch):
        monkeypatch.setenv("AXIOM_INDEX_CACHE", "0")
        key = index_cache.fingerprint("k", [])
        index_cache.store("k", key, 1)
        monkeypatch.delenv("AXIOM_INDEX_CACHE")
        assert index_cache.load("k", key) is None

    def test_write_leaves_no_temp_files_and_prunes(self):
        for i in range(index_cache._KEEP + 4):
            index_cache.store("k", index_cache.fingerprint("k", [str(i)]), i)
        files = list(index_cache.cache_dir().iterdir())
        assert not [f for f in files if f.name.endswith(".tmp")]
        assert len(files) == index_cache._KEEP

    def test_unserialisable_payload_is_ignored(self):
        index_cache.store("k", index_cache.fingerprint("k", []), {"x": object()})
        assert not [f for f in index_cache.cache_dir().iterdir() if f.name.endswith(".tmp")]

    def test_clear(self):
        index_cache.store("k", index_cache.fingerprint("k", []), 1)
        assert index_cache.clear() == 1


class TestCliCommandCache:
    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        root = tmp_path / "exts"
        _write_ext(root, "alpha", "alpha")
        monkeypatch.setattr(discovery, "get_extension_dirs", lambda: [root])
        return root

    def test_cached_answer_equals_a_fresh_scan(self, tree):
        first = discovery.discover_cli_commands()
        again = discovery.discover_cli_commands()  # served from disk
        assert set(first) == {"alpha"}
        assert first == again == discovery._discover_cli_commands_uncached()
        assert list(index_cache.cache_dir().glob("cli-commands-*.json"))

    def test_a_new_extension_is_seen_at_once(self, tree):
        assert set(discovery.discover_cli_commands()) == {"alpha"}
        _write_ext(tree, "beta", "beta")
        assert set(discovery.discover_cli_commands()) == {"alpha", "beta"}

    def test_an_edited_manifest_is_seen_at_once(self, tree):
        assert discovery.discover_cli_commands()["alpha"]["description"] == "d"
        _write_ext(tree, "alpha", "alpha", description="a much longer description")
        assert discovery.discover_cli_commands()["alpha"]["description"] == "a much longer description"

    def test_a_removed_extension_is_gone_at_once(self, tree):
        _write_ext(tree, "beta", "beta")
        assert set(discovery.discover_cli_commands()) == {"alpha", "beta"}
        (tree / "beta" / "axiom-extension.toml").unlink()
        assert set(discovery.discover_cli_commands()) == {"alpha"}

    def test_explicit_dirs_never_touch_the_cache(self, tree):
        discovery.discover_cli_commands(tree)
        assert not index_cache.cache_dir().exists()

    def test_a_patched_scan_is_never_cached(self, tree, monkeypatch):
        monkeypatch.setattr(discovery, "discover_extensions", lambda *a: [])
        assert discovery.discover_cli_commands() == {}
        assert not index_cache.cache_dir().exists()

    def test_a_corrupt_cache_falls_back_to_the_scan(self, tree):
        discovery.discover_cli_commands()
        for f in index_cache.cache_dir().glob("cli-commands-*.json"):
            f.write_text("not json", encoding="utf-8")
        assert set(discovery.discover_cli_commands()) == {"alpha"}


class TestInstalledDirsCache:
    def test_second_call_does_not_rescan(self, monkeypatch):
        calls = []
        real = discovery._scan_installed_package_extension_dirs

        def counting():
            calls.append(1)
            out = real()
            return out

        monkeypatch.setattr(discovery, "_scan_installed_package_extension_dirs", counting)
        first = discovery._installed_package_extension_dirs()
        second = discovery._installed_package_extension_dirs()
        assert first == second
        assert len(calls) == 1

    def test_a_scan_cut_short_is_not_remembered(self, monkeypatch):
        def cut_short():
            discovery._scan_completed = False
            return []

        monkeypatch.setattr(discovery, "_scan_installed_package_extension_dirs", cut_short)
        assert discovery._installed_package_extension_dirs() == []
        d = index_cache.cache_dir()
        assert not (d.exists() and list(d.glob("installed-ext-dirs-*.json")))

    def test_a_changed_site_directory_invalidates(self, tmp_path, monkeypatch):
        site = tmp_path / "site-packages"
        site.mkdir()
        monkeypatch.setattr("sys.path", [str(site)])
        before = index_cache.fingerprint("x", discovery._site_fingerprint_parts())
        (site / "newpkg-1.0.dist-info").mkdir()
        after = index_cache.fingerprint("x", discovery._site_fingerprint_parts())
        assert before != after
        (site / "editable.pth").write_text("/some/path\n")
        with_pth = index_cache.fingerprint("x", discovery._site_fingerprint_parts())
        assert with_pth != after

    def test_cached_file_is_plain_json(self):
        discovery._installed_package_extension_dirs()
        for f in index_cache.cache_dir().glob("installed-ext-dirs-*.json"):
            assert json.loads(f.read_text())["schema"] == index_cache.SCHEMA
