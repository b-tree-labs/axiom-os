# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The cached completion tree must complete exactly what the eager tree did."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

import pytest

from axiom.cli import completion_index as ci


def _sample_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="axi")
    sub = top.add_subparsers(dest="subcommand")
    sig = sub.add_parser("signal", help="Signals", description="Signal verbs")
    sig.add_argument("--verbose", "-v", action="store_true", help="loud")
    sig.add_argument("--level", choices=["a", "b"], help="how much")
    sig.add_argument("target", nargs="?", help="what")
    inner = sig.add_subparsers(dest="verb")
    brief = inner.add_parser("brief", help="Brief me")
    brief.add_argument("--since", metavar="WHEN")
    inner.add_parser("status", help="Status")
    sub.add_parser("plain", help="No flags")
    sub.add_parser("git-only", help="Needs git")
    return top


def _names(parser: argparse.ArgumentParser) -> set[str]:
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return set(a.choices)
    return set()


class TestSerialisation:
    def test_roundtrip_keeps_verbs_flags_choices_and_nesting(self):
        doc = ci.build_doc(_sample_parser(), {})
        rebuilt = ci.build_parser(doc, prog="axi", available=lambda r: True)
        assert _names(rebuilt) == {"signal", "plain", "git-only"}
        sig = next(a for a in rebuilt._actions if isinstance(a, argparse._SubParsersAction)).choices["signal"]
        flags = {o for a in sig._actions for o in a.option_strings}
        assert {"--verbose", "-v", "--level", "-h", "--help"} <= flags
        level = next(a for a in sig._actions if "--level" in a.option_strings)
        assert list(level.choices) == ["a", "b"]
        assert _names(sig) == {"brief", "status"}

    def test_document_is_plain_json(self):
        import json

        doc = ci.build_doc(_sample_parser(), {"git-only": ["git"]})
        assert json.loads(json.dumps(doc)) == doc

    def test_gating_is_applied_when_the_tree_is_read_not_baked_in(self):
        doc = ci.build_doc(_sample_parser(), {"git-only": ["git"]})
        absent = ci.build_parser(doc, prog="axi", available=lambda r: "git" not in r)
        present = ci.build_parser(doc, prog="axi", available=lambda r: True)
        assert "git-only" not in _names(absent)
        assert "git-only" in _names(present)

    def test_show_unavailable_overrides_the_gate(self, monkeypatch):
        monkeypatch.setenv("AXI_SHOW_UNAVAILABLE", "1")
        doc = ci.build_doc(_sample_parser(), {"git-only": ["git"]})
        assert "git-only" in _names(ci.build_parser(doc, prog="axi", available=lambda r: False))


class TestFingerprint:
    def test_resolving_a_verb_source_does_not_import_it(self):
        f = ci._module_file("axiom.cli.role", None)
        assert f and f.endswith(os.path.join("axiom", "cli", "role.py"))
        assert os.path.isfile(f)

    def test_an_extension_module_is_found_from_its_root(self, tmp_path):
        root = tmp_path / "pkg" / "extensions" / "builtins" / "thing"
        root.mkdir(parents=True)
        (root / "cli.py").write_text("x = 1\n")
        f = ci._module_file("pkg.extensions.builtins.thing.cli", str(root))
        assert f == str(root / "cli.py")

    def test_editing_a_verb_source_changes_the_fingerprint(self, tmp_path, monkeypatch):
        from axiom.extensions import discovery, index_cache

        root = tmp_path / "pkg" / "extensions" / "builtins" / "thing"
        root.mkdir(parents=True)
        src = root / "cli.py"
        src.write_text("x = 1\n")
        monkeypatch.setattr(discovery, "get_extension_dirs", lambda: [])
        ext = {"thing": {"module": "pkg.extensions.builtins.thing.cli", "root": str(root)}}
        before = index_cache.fingerprint(ci.KIND, ci.fingerprint_parts({}, ext, __file__))
        src.write_text("x = 22222\n")
        after = index_cache.fingerprint(ci.KIND, ci.fingerprint_parts({}, ext, __file__))
        assert before != after


def _complete(line: str, state_dir: str, *, cache: bool) -> list[str]:
    fd, out = tempfile.mkstemp(suffix=".completions")
    os.close(fd)
    env = {
        **os.environ,
        "AXI_STATE_DIR": state_dir,
        "AXIOM_INDEX_CACHE": "1" if cache else "0",
        "AXI_NO_SOURCE_WARNING": "1",
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\x0b",
        "COMP_LINE": line,
        "COMP_POINT": str(len(line)),
        "_ARGCOMPLETE_STDOUT_FILENAME": out,
    }
    try:
        subprocess.run([sys.executable, "-m", "axiom.axiom_cli"], env=env, capture_output=True, timeout=300)
        with open(out, encoding="utf-8", errors="replace") as f:
            return sorted(c.strip() for c in f.read().split("\x0b") if c.strip())
    finally:
        os.unlink(out)


@pytest.mark.parametrize("line", ["axi ", "axi signal ", "axi ext ", "axi -", "axi ext l", "axi signal --"])
def test_cached_tree_completes_the_same_as_the_eager_tree(line, tmp_path_factory):
    pytest.importorskip("argcomplete")
    state = str(tmp_path_factory.getbasetemp() / "completion-equivalence-state")
    eager = _complete(line, state, cache=False)
    cold = _complete(line, state, cache=True)  # builds and stores the tree
    warm = _complete(line, state, cache=True)  # reads it
    assert eager, f"eager completion of {line!r} returned nothing"
    assert cold == eager
    assert warm == eager
