# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A command line you can explore: Tab with the site's values, ``explore``, ``next:`` lines.

Every test runs against a temporary home and state directory. None reads or
writes the real ``~/.axi`` or a real shell start-up file, and none reaches a
network: the value cache is seeded or filled by fake providers.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.cli import command_space as cs
from axiom.cli import completion_index as ci
from axiom.cli import completion_values as cv
from axiom.cli import next_steps


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setenv(cv.CACHE_ENV, str(state / "cache" / cv.CACHE_NAME))
    monkeypatch.setenv("AXI_COMPLETION_NO_REFRESH", "1")
    monkeypatch.setattr(cv, "local_binding", lambda: {})
    return SimpleNamespace(home=home, state=state)


def _seed(**kw) -> cv.ValueCache:
    cache = cv.ValueCache(
        fetched_at=time.time(),
        binding={"site": "example-loop", "face_url": "https://node.example.org"},
        values={
            "site": ["example-loop", "example-bench"],
            "role": ["fluid_temperature", "pump_speed"],
            "channel": ["TC1", "TC2"],
            "feed": ["example.epics"],
            "day": ["2026-10-01", "2026-09-30"],
        },
        per_site={
            "example-loop": {"role": ["fluid_temperature"], "channel": ["TC1"], "day": ["2026-10-01"]},
            "example-bench": {"role": ["pump_speed"], "channel": ["TC2"], "day": ["2026-09-30"]},
        },
    )
    for k, v in kw.items():
        setattr(cache, k, v)
    cv.save(cache)
    return cache


# --------------------------------------------------------------------------- a small CLI


def _parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="axi")
    sub = top.add_subparsers(dest="subcommand")
    data = sub.add_parser("data", help="Ask a site's data")
    verbs = data.add_subparsers(dest="verb")
    verbs.add_parser("roles", help="Which quantities the fleet can answer.")
    ch = verbs.add_parser("channels", help="What channels does a site have?")
    ch.add_argument("--site")
    ch.add_argument("--role")
    show = verbs.add_parser("show", help="Show me a role or a channel. The latest day.")
    show.add_argument("target")
    show.add_argument("--site")
    show.add_argument("--feed")
    show.add_argument("--day")
    agg = verbs.add_parser("aggregate", help="One aggregate.")
    agg.add_argument("--table", required=True)
    agg.add_argument("--site")
    wipe = verbs.add_parser("wipe", help="Delete everything.")
    wipe.add_argument("--site")
    return top


class _Spec(SimpleNamespace):
    @property
    def effective_risk(self):
        return self.risk


SPECS = {
    "data.roles": _Spec(inputs={}, values=None, risk="read"),
    "data.channels": _Spec(inputs={"site": "str", "role": "str"}, values=None, risk="read"),
    "data.show": _Spec(inputs={"target": "str!", "site": "str", "feed": "str", "day": "str"},
                       values={"target": "role|channel"}, risk="read"),
    "data.aggregate": _Spec(inputs={"table": "str!", "site": "str"}, values=None, risk="read"),
    "data.wipe": _Spec(inputs={"site": "str"}, values=None, risk="destructive"),
}


@pytest.fixture
def doc():
    return cs.annotate(ci.build_doc(_parser(), {}), SPECS)


@pytest.fixture
def space(doc):
    return cs.from_doc(doc)


# --------------------------------------------------------------------------- the value cache


class TestValueCache:
    def test_cold_cache_answers_nothing_and_starts_one_refresh(self, monkeypatch):
        monkeypatch.delenv("AXI_COMPLETION_NO_REFRESH")
        spawned: list[list[str]] = []
        monkeypatch.setattr(cv.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
        assert cv.values_for(("role",)) == []
        assert cv.values_for(("role",)) == [], "a second press in the burst"
        assert len(spawned) == 1, "a burst of presses starts one refresh, not one each"
        assert spawned[0][1:] == ["-m", "axiom.cli.completion_values", "refresh"]

    def test_cold_cache_still_completes_the_bound_site_from_the_local_file(self, monkeypatch):
        monkeypatch.setattr(cv, "local_binding", lambda: {"site": "example-loop", "face_url": "x"})
        assert cv.values_for(("site",)) == ["example-loop"]

    def test_warm_cache_answers_without_a_refresh(self, monkeypatch):
        monkeypatch.delenv("AXI_COMPLETION_NO_REFRESH")
        _seed()
        monkeypatch.setattr(cv.subprocess, "Popen", lambda *a, **k: pytest.fail("fresh cache refreshed"))
        assert cv.values_for(("role", "channel")) == ["fluid_temperature", "pump_speed", "TC1", "TC2"]

    def test_a_typed_site_narrows_per_site_kinds(self):
        _seed()
        assert cv.values_for(("channel",), site="example-bench") == ["TC2"]
        assert cv.values_for(("channel",), site="unknown-site") == ["TC1", "TC2"]

    def test_a_stale_cache_answers_and_refreshes_in_the_background(self, monkeypatch):
        monkeypatch.delenv("AXI_COMPLETION_NO_REFRESH")
        _seed(fetched_at=time.time() - 10 * cv.DEFAULT_TTL_S)
        spawned: list[object] = []
        monkeypatch.setattr(cv.subprocess, "Popen", lambda *a, **k: spawned.append(a))
        assert "fluid_temperature" in cv.values_for(("role",))
        assert len(spawned) == 1

    def test_the_completer_filters_by_prefix_and_reads_the_typed_site(self):
        _seed()
        comp = cv.LiveValues(("role", "channel"))
        assert comp(prefix="T", parsed_args=argparse.Namespace(site="example-loop")) == ["TC1"]
        assert comp(prefix="p") == ["pump_speed"]

    def test_a_corrupt_cache_is_an_empty_one(self):
        path = cv.cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert cv.load().empty
        assert cv.LiveValues(("role",))(prefix="") == []

    def test_an_offline_node_keeps_what_tab_knew(self):
        _seed()

        def offline(_ctx):
            raise ConnectionError("node did not answer")

        fresh = cv.refresh(use=[("data", offline)])
        assert any("node did not answer" in e for e in fresh.errors)
        assert cv.load().get("role") == ["fluid_temperature", "pump_speed"], "an offline refresh erased values"

    def test_a_partial_failure_keeps_only_the_failed_kinds(self):
        _seed()

        def half(_ctx):
            return {"values": {"role": ["new_role"], "channel": []}, "failed_kinds": ["channel"],
                    "errors": ["channel: timed out"]}

        cv.refresh(use=[("data", half)])
        cache = cv.load()
        assert cache.get("role") == ["new_role"]
        assert cache.get("channel") == ["TC1", "TC2"]

    def test_the_cache_lives_in_the_state_dir_not_the_working_directory(self, tmp_path, monkeypatch):
        monkeypatch.delenv(cv.CACHE_ENV)
        monkeypatch.chdir(tmp_path)
        assert cv.cache_path().is_relative_to(Path(os.environ["AXI_STATE_DIR"]))
        cv.refresh(use=[])
        assert not any(p.name == cv.CACHE_NAME for p in tmp_path.iterdir())


# --------------------------------------------------------------------------- one grammar


class TestTheRegistryJoin:
    def test_a_skills_declared_kind_wins_over_the_parameter_name(self, space):
        target = next(a for a in space.find(("data", "show")).args if a.key == "target")
        assert target.values == ("role", "channel")

    def test_a_parameter_named_site_completes_sites(self, space):
        site = next(a for a in space.find(("data", "channels")).args if a.key == "site")
        assert site.values == ("site",)

    def test_verbs_carry_their_skill_and_risk(self, space):
        assert space.find(("data", "show")).skill == "data.show"
        assert space.find(("data", "wipe")).risk == "destructive"

    def test_the_cached_tree_attaches_live_completers(self, doc):
        parser = ci.build_parser(doc, prog="axi", available=lambda r: True)
        data = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices["data"]
        show = next(a for a in data._actions if isinstance(a, argparse._SubParsersAction)).choices["show"]
        target = next(a for a in show._actions if a.dest == "target")
        assert isinstance(target.completer, cv.LiveValues)
        assert target.completer.kinds == ("role", "channel")

    def test_required_flags_survive_the_round_trip(self, doc):
        agg = doc["commands"][0]["tree"]["subs"]["aggregate"]
        assert next(a for a in agg["args"] if a["names"] == ["--table"]).get("required") is True


class TestExamples:
    def lookup(self):
        return next_steps.lookup_from(_seed())

    def test_an_example_uses_real_values_and_the_site_first(self, space):
        ex = cs.example_for(space.find(("data", "show")), self.lookup())
        assert ex.line("neut") == "neut data show fluid_temperature --site example-loop --day 2026-10-01"

    def test_a_required_value_nobody_knows_means_no_example(self, space):
        assert cs.example_for(space.find(("data", "aggregate")), self.lookup()) is None

    def test_no_values_means_no_placeholder(self, space):
        empty = next_steps.lookup_from(cv.ValueCache())
        assert cs.example_for(space.find(("data", "show")), empty) is None
        assert cs.example_for(space.find(("data", "roles")), empty).line("axi") == "axi data roles"

    def test_a_disambiguating_feed_is_left_out(self, space):
        assert "--feed" not in cs.example_for(space.find(("data", "show")), self.lookup()).argv

    def test_the_chat_phrasing_carries_the_values(self, space):
        node = space.find(("data", "show"))
        phrase = cs.chat_phrase(node, cs.example_for(node, self.lookup()))
        assert phrase == "show me a role or a channel: fluid_temperature at example-loop on 2026-10-01"


# --------------------------------------------------------------------------- next steps


class TestNextSteps:
    def test_a_noun_suggests_read_verbs_filled_with_real_values(self, space):
        lines = next_steps.suggest(("data",), space=space, cache=_seed(), recent=[], cli="neut",
                                   prefer=("roles", "channels", "show"))
        assert lines == [
            "neut data roles",
            "neut data channels --site example-loop --role fluid_temperature",
            "neut data show fluid_temperature --site example-loop --day 2026-10-01",
        ]

    def test_a_destructive_verb_is_never_suggested(self, space):
        lines = next_steps.suggest(("data",), space=space, cache=_seed(), recent=[], limit=10)
        assert not any("wipe" in ln for ln in lines)

    def test_nothing_that_needs_an_unknown_value(self, space):
        lines = next_steps.suggest(("data",), space=space, cache=cv.ValueCache(), recent=[], cli="axi")
        # Both run as written (the site defaults to the binding); `show` needs a target nobody named.
        assert sorted(lines) == ["axi data channels", "axi data roles"]
        assert not any("<" in ln or "show" in ln for ln in lines)

    def test_a_verbs_help_starts_with_its_own_example(self, space):
        lines = next_steps.suggest(("data", "show"), space=space, cache=_seed(), recent=[], cli="axi")
        assert lines[0].startswith("axi data show fluid_temperature")

    def test_recent_history_ranks_first(self, space):
        recent = [{"path": ["data", "channels"], "at": 1}]
        lines = next_steps.suggest(("data",), space=space, cache=_seed(), recent=recent, cli="axi")
        assert lines[0].startswith("axi data channels")

    def test_history_records_the_noun_and_verb_never_an_argument(self, tmp_path):
        path = tmp_path / "h.json"
        next_steps.record(["chat", "what is my password hunter2"], path=path)
        next_steps.record(["data", "show", "fluid_temperature", "--site", "example-loop"], path=path)
        next_steps.record(["vault", "set", "my-secret-name"], path=path)
        text = path.read_text()
        assert "hunter2" not in text and "fluid_temperature" not in text and "my-secret-name" not in text
        assert [e["path"] for e in next_steps.history(path=path)] == [["vault", "set"], ["data", "show"], ["chat"]]

    def test_help_path(self):
        assert next_steps.help_path("data", ["show", "x", "--help"]) == ("data", "show")
        assert next_steps.help_path("data", ["-h"]) == ("data",)
        assert next_steps.help_path("data", ["show"]) is None

    def test_the_block_is_the_one_data_results_use(self):
        from axiom.extensions.builtins.data_platform import friendly

        assert friendly.next_block(["a", "b"]) == next_steps.block(["a", "b"]) == "\n\nnext:\n  a\n  b"


# --------------------------------------------------------------------------- explore


class TestExplore:
    def test_the_model_lists_verbs_with_examples_and_chat(self, space):
        from axiom.cli import explore

        scr = explore.screen(("data",), space=space, cache=_seed(), cli="neut")
        by = {i.name: i for i in scr.items}
        assert set(by) == {"roles", "channels", "show", "aggregate", "wipe"}
        assert by["show"].example == "neut data show fluid_temperature --site example-loop --day 2026-10-01"
        assert by["show"].ask.startswith('neut chat --ask "show me a role or a channel: fluid_temperature')
        assert by["aggregate"].example is None
        assert by["show"].runnable and not by["wipe"].runnable

    def test_a_verb_screen_lists_its_flags(self, space):
        from axiom.cli import explore

        scr = explore.screen(("data", "show"), space=space, cache=_seed(), cli="axi")
        names = [i.name for i in scr.items]
        assert "--site" in names and "target" in names
        assert all(i.kind == "flag" for i in scr.items)

    def test_the_listing_marks_what_changes_things(self, space):
        from axiom.cli import explore

        text = explore.render_listing(explore.screen(("data",), space=space, cache=_seed(), cli="axi"))
        wipe = next(ln for ln in text.splitlines() if "axi data wipe" in ln)
        assert "changes things" in wipe
        assert "changes things" not in next(ln for ln in text.splitlines() if "$ axi data roles" in ln)

    def test_an_unknown_path_says_what_is_there(self, space):
        from axiom.cli import explore

        scr = explore.screen(("data", "nope"), space=space, cache=_seed(), cli="axi")
        assert not scr.items and "roles" in scr.note

    def test_not_a_tty_prints_the_listing(self, space, monkeypatch, capsys):
        from axiom.cli import explore

        monkeypatch.setattr(cs, "load", lambda build=False: space)
        _seed()
        assert explore.main(["data"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("axi data")
        assert "$ axi data channels --site example-loop --role fluid_temperature" in out

    def test_json(self, space, monkeypatch, capsys):
        from axiom.cli import explore

        monkeypatch.setattr(cs, "load", lambda build=False: space)
        assert explore.main(["data", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["path"] == ["data"] and data["items"]


# --------------------------------------------------------------------------- the setup step


def _fake_run(calls, *, registered=True, answers=True):
    def run(argv, **kw):
        calls.append(argv)
        out_file = (kw.get("env") or {}).get("_ARGCOMPLETE_STDOUT_FILENAME")
        if out_file and answers:
            Path(out_file).write_text("data\nexplore\n", encoding="utf-8")
        stdout = "1\n" if registered else "0\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
    return run


def _env(home, state, *, shell="zsh", system="darwin", **seams):
    from axiom.cli.completion_setup import CompletionEnv

    which = seams.pop("which", lambda name: f"/usr/bin/{name}")
    return CompletionEnv(home=home, state_dir=state, system=system, cli="neut",
                         say=lambda _m: None,
                         seams={"shell": shell, "which": which, "no_refresh": True, **seams})


class TestCompletionStep:
    def test_check_is_red_until_applied_and_writes_nothing(self, _isolated):
        from axiom.cli.completion_setup import CompletionStep

        env = _env(_isolated.home, _isolated.state, run=_fake_run([]))
        line = CompletionStep().check(env)
        assert not line.green and "completion setup" in line.fix
        assert not (_isolated.home / ".zshrc").exists(), "check (what doctor runs) wrote a file"

    def test_apply_adds_one_marked_block_and_verifies(self, _isolated):
        from axiom.cli.completion_setup import CompletionStep

        calls: list[list[str]] = []
        env = _env(_isolated.home, _isolated.state, run=_fake_run(calls))
        rc = _isolated.home / ".zshrc"
        rc.write_text("export EDITOR=vim\n", encoding="utf-8")
        line = CompletionStep().apply(env)
        assert line.ok, line
        text = rc.read_text()
        assert text.startswith("export EDITOR=vim\n")
        assert text.count("# >>> neut tab completion (added by neut setup) >>>") == 1
        assert (_isolated.state / "completion" / "neut.zsh").read_text().strip()
        assert any(c[0].endswith("zsh") and "-i" in c for c in calls), "verified in a new interactive shell"
        assert any(len(c) == 1 and c[0].endswith("neut") for c in calls), "the CLI answered the protocol"
        assert list(_isolated.home.glob(".zshrc.completion-setup-bak-*")), "a backup was made first"

    def test_apply_twice_never_stacks_a_second_block(self, _isolated):
        from axiom.cli.completion_setup import CompletionStep

        env = _env(_isolated.home, _isolated.state, run=_fake_run([]))
        CompletionStep().apply(env)
        CompletionStep().apply(env)
        assert (_isolated.home / ".zshrc").read_text().count("tab completion (added by") == 1

    def test_the_vault_block_and_the_completion_block_live_side_by_side(self, _isolated):
        from axiom.cli.completion_setup import CompletionStep
        from axiom.extensions.builtins.secrets import shell_profiles as sp

        rc = _isolated.home / ".zshrc"
        sp.apply("zsh", rc, sp.render("zsh", url="http://127.0.0.1:8200", token_file="/t", product="Neut"))
        CompletionStep().apply(_env(_isolated.home, _isolated.state, run=_fake_run([])))
        text = rc.read_text()
        assert "AXIOM_OPENBAO_URL" in text and "tab completion" in text
        assert sp._find_block(text) is not None, "the vault block is still found by its own markers"

    def test_a_half_block_is_refused_untouched(self, _isolated):
        from axiom.cli.completion_setup import CompletionStep

        rc = _isolated.home / ".zshrc"
        rc.write_text("# >>> neut tab completion (added by neut setup) >>>\nmine\n", encoding="utf-8")
        line = CompletionStep().apply(_env(_isolated.home, _isolated.state, run=_fake_run([])))
        assert not line.ok and "not changed" in line.status
        assert rc.read_text() == "# >>> neut tab completion (added by neut setup) >>>\nmine\n"

    def test_registered_but_silent_cli_is_red(self, _isolated):
        from axiom.cli.completion_setup import CompletionStep

        env = _env(_isolated.home, _isolated.state, run=_fake_run([], answers=False))
        line = CompletionStep().apply(env)
        assert not line.ok and "answered a completion with nothing" in line.status

    @pytest.mark.parametrize("shell,system,where", [
        ("bash", "linux", ".bashrc"),
        ("bash", "darwin", ".bash_profile"),
        ("fish", "linux", ".config/fish/conf.d/neut-completion.fish"),
    ])
    def test_each_shell_gets_its_own_start_up_file(self, _isolated, shell, system, where):
        from axiom.cli.completion_setup import CompletionStep

        env = _env(_isolated.home, _isolated.state, shell=shell, system=system, run=_fake_run([]))
        CompletionStep().apply(env)
        assert "tab completion" in (_isolated.home / where).read_text()

    @pytest.mark.parametrize("system,term,names", [
        ("darwin", "", ("iTerm2", "Ghostty", "zsh")),
        ("windows", "", ("Windows Terminal", "WSL")),
        ("linux", "", ("zsh", "fish")),
        ("linux", "vscode", ("VS Code",)),
    ])
    def test_a_shell_that_cannot_complete_names_a_terminal_that_can(self, _isolated, system, term, names):
        from axiom.cli.completion_setup import CompletionStep

        env = _env(_isolated.home, _isolated.state, shell="cmd" if system == "windows" else "sh",
                   system=system, environ={"TERM_PROGRAM": term})
        line = CompletionStep().check(env)
        assert not line.green
        for name in names:
            assert name in line.fix
        assert CompletionStep().plan(env) is None, "nothing setup can do: the remedy is the person's"

    def test_a_cli_not_on_path_is_red_with_its_reason(self, _isolated):
        from axiom.cli.completion_setup import CompletionStep

        env = _env(_isolated.home, _isolated.state, run=_fake_run([]),
                   which=lambda n: None if n == "neut" else f"/usr/bin/{n}")
        line = CompletionStep().apply(env)
        assert not line.ok and "not on this shell's PATH" in line.status

    def test_the_step_fits_the_setup_walk(self):
        from axiom.cli.completion_setup import setup_step

        step = setup_step()
        assert step.name == "completion" and step.title
        for method in ("check", "plan", "apply"):
            assert callable(getattr(step, method))

    def test_doctor_shows_the_row_exactly_once_and_changes_nothing(self, monkeypatch, _isolated):
        import axiom.axiom_cli as cli
        from axiom.extensions.builtins.setup import runner, steps

        assert [s.name for s in steps.builtin_steps()].count("completion") == 1
        monkeypatch.setattr(runner, "all_steps", lambda: [s for s in steps.builtin_steps()
                                                           if s.name == "completion"])
        monkeypatch.setattr(
            runner, "resolve_env",
            lambda params=None: _env(_isolated.home, _isolated.state, run=_fake_run([])))
        rows = [r for r in cli._setup_checks() if r["name"] == "Setup: Tab completion"]
        assert len(rows) == 1 and rows[0]["ok"] is False and "completion setup" in rows[0]["fix"]
        assert not (_isolated.home / ".zshrc").exists(), "doctor wrote a start-up file"

    def test_the_setup_walk_applies_it_like_any_step(self, _isolated):
        from axiom.extensions.builtins.setup import runner, steps

        step = next(s for s in steps.builtin_steps() if s.name == "completion")
        assert isinstance(step, steps.Step)
        from axiom.extensions.builtins.setup.environment import SetupEnv

        mine = _env(_isolated.home, _isolated.state, run=_fake_run([]))
        env = SetupEnv(home=mine.home, state_dir=mine.state_dir, system=mine.system, cli="neut",
                       say=lambda _m: None, seams=mine.seams)
        out = runner.run_steps(env, [step], mode="apply", ask=lambda _q: True)
        assert out["lines"][0].ok, out["lines"][0]
        assert "tab completion" in (_isolated.home / ".zshrc").read_text()


# --------------------------------------------------------------------------- the CLI


class TestTheCli:
    def test_a_private_parser_builder_is_found(self):
        import axiom.axiom_cli as cli

        mod = SimpleNamespace(_parser=lambda: argparse.ArgumentParser(prog="x"))
        assert cli._parser_builder(mod) is mod._parser

    def test_nested_verbs_and_required_flags_reach_the_tree(self):
        import axiom.axiom_cli as cli

        src = argparse.ArgumentParser(prog="data")
        sub = src.add_subparsers(dest="verb")
        con = sub.add_parser("connector")
        csub = con.add_subparsers(dest="connector_verb")
        add = csub.add_parser("add")
        add.add_argument("--name", required=True)
        dst = argparse.ArgumentParser(prog="axi data")
        cli._copy_subparsers(src, dst)
        tree = ci.serialize_parser(dst)
        add_doc = tree["subs"]["connector"]["subs"]["add"]
        assert add_doc["args"][0]["required"] is True

    def test_the_explore_and_completion_nouns_exist(self):
        import axiom.axiom_cli as cli

        assert cli.SUBCOMMANDS["explore"] == "axiom.cli.explore"
        assert cli.SUBCOMMANDS["completion"] == "axiom.cli.completion_cmd"


def test_tab_completes_the_sites_real_values_end_to_end(tmp_path, monkeypatch):
    """The real CLI, the real argcomplete protocol, a seeded cache: values come back."""
    pytest.importorskip("argcomplete")
    state = tmp_path / "e2e-state"
    cache = state / "cache" / cv.CACHE_NAME
    monkeypatch.setenv(cv.CACHE_ENV, str(cache))
    _seed()

    def tab(line: str) -> list[str]:
        fd, out = tempfile.mkstemp(suffix=".completions")
        os.close(fd)
        env = {**os.environ, "AXI_STATE_DIR": str(state), "HOME": str(tmp_path),
               cv.CACHE_ENV: str(cache), "AXI_NO_SOURCE_WARNING": "1", "AXI_COMPLETION_NO_REFRESH": "1",
               "_ARGCOMPLETE": "1", "_ARGCOMPLETE_IFS": "\x0b", "COMP_LINE": line,
               "COMP_POINT": str(len(line)), "_ARGCOMPLETE_STDOUT_FILENAME": out}
        try:
            subprocess.run([sys.executable, "-m", "axiom.axiom_cli"], env=env, capture_output=True, timeout=300)
            return sorted(c.strip() for c in Path(out).read_text().split("\x0b") if c.strip())
        finally:
            os.unlink(out)

    assert "show" in tab("axi data sh"), "the data noun's verbs are in the tree"
    assert {"fluid_temperature", "TC1"} <= set(tab("axi data show "))
    assert tab("axi data channels --site example-loop --role ") == ["fluid_temperature"]
    cache.unlink()
    cold = tab("axi data show --s")
    assert "--site" in cold, "a cold cache still completes flags"


def test_a_flag_example_starts_from_the_shortest_line(space):
    from axiom.cli import explore

    scr = explore.screen(("data", "show"), space=space, cache=_seed(), cli="axi")
    day = next(i for i in scr.items if i.name == "--day")
    site = next(i for i in scr.items if i.name == "--site")
    assert day.example == "axi data show fluid_temperature --day 2026-10-01"
    assert site.example == "axi data show fluid_temperature --site example-loop"


def test_e_g_does_not_end_a_sentence():
    assert cs.first_sentence("bucket size, e.g. 15m, 1h. More.") == "bucket size, e.g. 15m, 1h"
