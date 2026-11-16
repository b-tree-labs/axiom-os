# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A manifest is parsed once until it changes.

Measured on a site node on 2026-11-13: every ``get_all_tools()`` re-discovered the extensions and
re-parsed about 186 ``axiom-extension.toml`` files (145-190 ms per call, 690 ms cold), and
``_build_system_prompt()`` did it again (about 150 ms). A chat turn calls them several times, so
each web chat turn spent a few hundred milliseconds re-reading files that had not changed.

``load_toml_cached`` keeps the parsed data and re-parses a file only when its modification time,
size or inode changes, so an edited, replaced or new manifest is seen on the next call with no
schedule and no restart. Each caller gets its own copy.
"""

from __future__ import annotations

import os
import tomllib

import pytest

from axiom.infra import toml_compat as tc


@pytest.fixture(autouse=True)
def _fresh_cache():
    tc.clear_toml_cache()
    yield
    tc.clear_toml_cache()


@pytest.fixture
def parses(monkeypatch):
    """Count real parses."""
    count = {"n": 0}
    real = tomllib.loads

    def counting(text, **kw):
        count["n"] += 1
        return real(text, **kw)

    monkeypatch.setattr(tc.tomllib, "loads", counting)
    return count


def _write(path, text, *, bump_ns=0):
    path.write_text(text, encoding="utf-8")
    if bump_ns:
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump_ns))


def test_tc1_an_unchanged_file_is_parsed_once(tmp_path, parses):
    p = tmp_path / "axiom-extension.toml"
    _write(p, '[extension]\nname = "a"\n')
    for _ in range(5):
        assert tc.load_toml_cached(p)["extension"]["name"] == "a"
    assert parses["n"] == 1


def test_tc2_an_edited_file_is_parsed_again(tmp_path, parses):
    p = tmp_path / "axiom-extension.toml"
    _write(p, '[extension]\nname = "a"\n')
    tc.load_toml_cached(p)
    _write(p, '[extension]\nname = "b"\n', bump_ns=1_000_000)
    assert tc.load_toml_cached(p)["extension"]["name"] == "b"
    assert parses["n"] == 2


def test_tc3_a_file_replaced_in_place_is_parsed_again(tmp_path, parses):
    p = tmp_path / "axiom-extension.toml"
    _write(p, '[extension]\nname = "a"\n')
    tc.load_toml_cached(p)
    new = tmp_path / "next.toml"
    _write(new, '[extension]\nname = "c"\n')
    st = p.stat()
    os.replace(new, p)  # an atomic install: a new inode, possibly the same size and time
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert tc.load_toml_cached(p)["extension"]["name"] == "c"


def test_tc4_each_caller_gets_its_own_copy(tmp_path):
    p = tmp_path / "axiom-extension.toml"
    _write(p, '[extension]\nname = "a"\ntags = ["x"]\n')
    first = tc.load_toml_cached(p)
    first["extension"]["name"] = "mutated"
    first["extension"]["tags"].append("y")
    again = tc.load_toml_cached(p)
    assert again["extension"] == {"name": "a", "tags": ["x"]}


def test_tc5_errors_are_raised_as_before_and_a_fixed_file_loads(tmp_path):
    p = tmp_path / "axiom-extension.toml"
    with pytest.raises(FileNotFoundError):
        tc.load_toml_cached(p)
    _write(p, "[extension\n")
    with pytest.raises(tomllib.TOMLDecodeError):
        tc.load_toml_cached(p)
    _write(p, '[extension]\nname = "ok"\n', bump_ns=1_000_000)
    assert tc.load_toml_cached(p)["extension"]["name"] == "ok"


def test_tc6_discovering_tools_twice_parses_each_manifest_at_most_once(parses):
    from axiom.extensions.builtins.chat.tools import get_all_tools

    get_all_tools()
    first = parses["n"]
    assert first > 0, "discovery reads manifests through the cache"
    get_all_tools()
    assert parses["n"] == first, f"the second discovery re-parsed {parses['n'] - first} manifests"


# --- skill scans: SKILL.md files are walked and read once per interval -------------------------
# After the manifest cache, most of a warm get_all_tools() was _scan_skills: a recursive walk of
# every extension's skills folder, reading each SKILL.md, on every call. A new skill file cannot
# be noticed without the walk, so the scan is reused for a short interval (production installs
# change tools by deploy, which restarts the process; a developer's new skill shows up within it).


def _skill(root, name, desc):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\ndescription: {desc}\n---\n# {name}\n", encoding="utf-8")


@pytest.fixture
def clock(monkeypatch):
    from axiom.extensions import contracts

    now = {"t": 1000.0}
    monkeypatch.setattr(contracts.time, "monotonic", lambda: now["t"])
    contracts.clear_skill_scan_cache()
    yield now
    contracts.clear_skill_scan_cache()


def test_tc7_a_skills_folder_is_walked_once_within_the_interval(tmp_path, clock, monkeypatch):
    from axiom.extensions import contracts

    _skill(tmp_path, "alpha", "first")
    walks = {"n": 0}
    real = contracts._walk_skill_files

    def counting(d):
        walks["n"] += 1
        return real(d)

    monkeypatch.setattr(contracts, "_walk_skill_files", counting)
    for _ in range(4):
        assert [s.name for s in contracts._scan_skills(tmp_path)] == ["alpha"]
    assert walks["n"] == 1


def test_tc8_a_new_skill_appears_after_the_interval(tmp_path, clock, monkeypatch):
    from axiom.extensions import contracts

    monkeypatch.setenv("AXI_SKILL_SCAN_TTL", "30")
    _skill(tmp_path, "alpha", "first")
    contracts._scan_skills(tmp_path)
    _skill(tmp_path, "beta", "second")
    clock["t"] += 10
    assert [s.name for s in contracts._scan_skills(tmp_path)] == ["alpha"], "within the interval"
    clock["t"] += 25
    assert [s.name for s in contracts._scan_skills(tmp_path)] == ["alpha", "beta"]


def test_tc9_an_interval_of_zero_always_walks(tmp_path, clock, monkeypatch):
    from axiom.extensions import contracts

    monkeypatch.setenv("AXI_SKILL_SCAN_TTL", "0")
    _skill(tmp_path, "alpha", "first")
    contracts._scan_skills(tmp_path)
    _skill(tmp_path, "beta", "second")
    assert [s.name for s in contracts._scan_skills(tmp_path)] == ["alpha", "beta"]


def test_tc10_each_caller_gets_its_own_skill_list(tmp_path, clock):
    from axiom.extensions import contracts

    _skill(tmp_path, "alpha", "first")
    first = contracts._scan_skills(tmp_path)
    first.clear()
    assert [s.description for s in contracts._scan_skills(tmp_path)] == ["first"]
