# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The acceptance harness: which spec rows a test proves, across repositories.

A row is proven only by a test that carries its ID: ``@pytest.mark.acceptance("MVP-02-A1")``
(or a ``test_mvp_02_a1_*`` name) in Python, ``it("MVP-02-A1 …")`` in TypeScript.
A row with no such test is ``unproven`` however much code exists for it, and a
tagged test that has not run is not proof either.
"""

from __future__ import annotations

import json
import logging
import textwrap
from pathlib import Path

import pytest

from axiom.extensions.builtins.app_buildout import acceptance as acc

SPEC = textwrap.dedent(
    """\
    # Spec

    ## P0 Foundation

    ### MVP-00 Shell

    | ID | Acceptance | Trace |
    |---|---|---|
    | MVP-00-A1 | The rail renders | R1 |
    | MVP-00-A2 | Off is absent | R2 |

    ### P0 rows outside the numbered items

    | ID | Acceptance | Trace |
    |---|---|---|
    | P0-ACC-A1 | `axi app acceptance --phase P1` lists every row | roadmap |

    ## P1 The MVP

    | Package | Items |
    |---|---|
    | P1.a Identity | MVP-07 |

    ### MVP-07 Auth

    | ID | Acceptance | Trace |
    |---|---|---|
    | MVP-07-A3 | Every denial is audited | MVP-13 |
    | MVP-07-A4 | A thing nobody built | — |

    ## The staging deploy (P1.f)

    | VAR-A9 | Not a real row header | x |
    """
)


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# The row registry is the spec
# --------------------------------------------------------------------------


class TestRows:
    def test_rows_come_from_the_spec_tables_with_their_phase_and_item(self):
        rows = acc.parse_rows(SPEC)
        ids = [r.id for r in rows]
        assert ids == ["MVP-00-A1", "MVP-00-A2", "P0-ACC-A1", "MVP-07-A3", "MVP-07-A4", "VAR-A9"]
        by = {r.id: r for r in rows}
        assert by["MVP-00-A1"].phase == "P0"
        assert by["P0-ACC-A1"].phase == "P0"
        assert by["MVP-07-A3"].phase == "P1"
        assert by["MVP-07-A3"].item == "MVP-07 Auth"
        assert by["MVP-07-A3"].text == "Every denial is audited"

    def test_a_duplicate_id_is_refused_by_name(self):
        dup = SPEC.replace("| MVP-07-A4 |", "| MVP-07-A3 |")
        with pytest.raises(acc.SpecError, match="MVP-07-A3"):
            acc.parse_rows(dup)


# --------------------------------------------------------------------------
# Finding the tests that carry an ID
# --------------------------------------------------------------------------


class TestScan:
    def test_python_markers_names_and_class_markers(self, tmp_path):
        _write(tmp_path, "tests/test_a.py", '''
            import pytest

            @pytest.mark.acceptance("MVP-00-A1")
            def test_rail():
                pass

            @pytest.mark.acceptance("MVP-00-A2", "MVP-07-A3")
            def test_two_rows():
                pass

            def test_mvp_07_a4_named_for_its_row():
                pass

            @pytest.mark.acceptance("MVP-00-A1")
            class TestGroup:
                def test_one(self):
                    pass

                def helper(self):
                    pass

            def test_untagged():
                """Mentions MVP-00-A1 in prose, which proves nothing."""
        ''')
        found = acc.scan_python(tmp_path, ["tests"], repo="axiom")
        assert {t.name for t in found["MVP-00-A1"]} == {"test_rail", "TestGroup::test_one"}
        assert [t.name for t in found["MVP-00-A2"]] == ["test_two_rows"]
        assert [t.name for t in found["MVP-07-A3"]] == ["test_two_rows"]
        assert [t.name for t in found["MVP-07-A4"]] == ["test_mvp_07_a4_named_for_its_row"]
        ref = found["MVP-00-A1"][0]
        assert ref.file == "tests/test_a.py" and ref.repo == "axiom"
        assert ref.nodeid in {"tests/test_a.py::test_rail", "tests/test_a.py::TestGroup::test_one"}

    def test_a_module_level_pytestmark_tags_every_test(self, tmp_path):
        _write(tmp_path, "tests/test_m.py", '''
            import pytest
            pytestmark = pytest.mark.acceptance("MVP-07-A3")

            def test_x():
                pass
        ''')
        assert [t.name for t in acc.scan_python(tmp_path, ["tests"])["MVP-07-A3"]] == ["test_x"]

    def test_typescript_titles_carry_the_id(self, tmp_path):
        _write(tmp_path, "frontend/src/Rail.test.tsx", '''
            describe("rail", () => {
              it("MVP-00-A1 renders the standard rail", () => {});
              test('MVP-00-A2 off is absent from search', () => {});
              it("renders without an id", () => {});
            });
        ''')
        _write(tmp_path, "frontend/node_modules/x/y.test.ts", 'it("MVP-00-A1 vendored", () => {})')
        found = acc.scan_js(tmp_path, ["frontend/src", "frontend"], repo="appkit")
        assert [t.name for t in found["MVP-00-A1"]] == ["MVP-00-A1 renders the standard rail"]
        assert [t.name for t in found["MVP-00-A2"]] == ["MVP-00-A2 off is absent from search"]
        assert found["MVP-00-A1"][0].file == "frontend/src/Rail.test.tsx"

    def test_a_file_that_does_not_parse_is_reported_not_fatal(self, tmp_path):
        _write(tmp_path, "tests/test_bad.py", "def test_(:\n")
        errors: list[str] = []
        assert acc.scan_python(tmp_path, ["tests"], errors=errors) == {}
        assert errors and "test_bad.py" in errors[0]


# --------------------------------------------------------------------------
# Results: pass / fail / unproven
# --------------------------------------------------------------------------


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_a" name="test_rail" time="0.1"/>
  <testcase classname="tests.test_a" name="test_two_rows[a]" time="0.1"/>
  <testcase classname="tests.test_a" name="test_two_rows[b]" time="0.1">
    <failure message="boom">boom</failure>
  </testcase>
  <testcase classname="tests.test_a.TestGroup" name="test_one" time="0.1">
    <skipped message="no db"/>
  </testcase>
</testsuite></testsuites>
"""


class TestResults:
    def test_junit_results_key_by_test_and_a_failed_parameter_fails_the_test(self, tmp_path):
        p = tmp_path / "r.xml"
        p.write_text(JUNIT, encoding="utf-8")
        res = acc.load_junit(p)
        assert res["tests/test_a.py::test_rail"] == "passed"
        assert res["tests/test_a.py::test_two_rows"] == "failed"
        assert res["tests/test_a.py::TestGroup::test_one"] == "skipped"

    def test_vitest_json_results_key_by_file_and_title(self, tmp_path):
        p = tmp_path / "v.json"
        p.write_text(json.dumps({"testResults": [{
            "name": str(tmp_path / "frontend/src/Rail.test.tsx"),
            "assertionResults": [
                {"title": "MVP-00-A1 renders the standard rail", "status": "passed"},
                {"title": "MVP-00-A2 off is absent from search", "status": "failed"},
            ],
        }]}), encoding="utf-8")
        res = acc.load_vitest_json(p, repo_root=tmp_path)
        assert res["frontend/src/Rail.test.tsx::MVP-00-A1 renders the standard rail"] == "passed"
        assert res["frontend/src/Rail.test.tsx::MVP-00-A2 off is absent from search"] == "failed"

    def test_row_status(self):
        t1 = acc.TestRef("axiom", "tests/test_a.py", "test_rail", "tests/test_a.py::test_rail")
        t2 = acc.TestRef("axiom", "tests/test_a.py", "test_two_rows",
                         "tests/test_a.py::test_two_rows")
        assert acc.row_status([], {})[0] == "unproven"
        assert acc.row_status([t1], {})[0] == "unproven"  # tagged, never run
        assert acc.row_status([t1], {"axiom": {t1.nodeid: "passed"}})[0] == "pass"
        assert acc.row_status([t1, t2], {"axiom": {t1.nodeid: "passed",
                                                   t2.nodeid: "failed"}})[0] == "fail"
        assert acc.row_status([t1], {"axiom": {t1.nodeid: "skipped"}})[0] == "unproven"
        # one passed and one not run: not yet proof
        assert acc.row_status([t1, t2], {"axiom": {t1.nodeid: "passed"}})[0] == "unproven"


# --------------------------------------------------------------------------
# The report across repositories
# --------------------------------------------------------------------------


def _workspace(tmp_path) -> Path:
    _write(tmp_path, "platform/docs/spec.md", SPEC)
    _write(tmp_path, "platform/tests/test_a.py", '''
        import pytest

        @pytest.mark.acceptance("MVP-00-A1")
        def test_rail():
            pass

        @pytest.mark.acceptance("MVP-07-A3")
        def test_denial():
            pass

        @pytest.mark.acceptance("MVP-99-A1")
        def test_for_a_row_that_does_not_exist():
            pass
    ''')
    _write(tmp_path, "ui/src/Rail.test.tsx", 'it("MVP-00-A2 off is absent", () => {});\n')
    cfg = _write(tmp_path, "acceptance.toml", '''
        [acceptance]
        spec = "platform/docs/spec.md"

        [[repo]]
        name = "platform"
        path = "platform"
        lang = "python"
        include = ["tests"]

        [[repo]]
        name = "ui"
        path = "ui"
        lang = "js"
        include = ["src"]
        results = "results.json"

        [[repo]]
        name = "site"
        path = "not-cloned"
        lang = "python"
        include = ["tests"]
    ''')
    return cfg


@pytest.mark.acceptance("P0-ACC-A1")
def test_p0_acc_a1_lists_every_row_with_its_tests_and_pass_fail_unproven(tmp_path):
    """P0-ACC-A1: every row of the phase, its tests across repos, and one of pass / fail / unproven."""
    cfg_path = _workspace(tmp_path)
    cfg = acc.load_config(cfg_path)
    cached = {"platform": {"tests/test_a.py::test_rail": "passed",
                           "tests/test_a.py::test_denial": "failed"}}
    report = acc.build_report(cfg, phase="P0", cached=cached)
    rows = {r["id"]: r for r in report["rows"]}
    assert list(rows) == ["MVP-00-A1", "MVP-00-A2", "P0-ACC-A1"]  # P0 only
    assert rows["MVP-00-A1"]["status"] == "pass"
    assert rows["MVP-00-A1"]["tests"][0]["repo"] == "platform"
    assert rows["MVP-00-A2"]["status"] == "unproven"  # tagged in ui, no result yet
    assert rows["MVP-00-A2"]["tests"][0]["repo"] == "ui"
    assert rows["P0-ACC-A1"]["status"] == "unproven" and rows["P0-ACC-A1"]["tests"] == []
    assert report["totals"] == {"pass": 1, "fail": 0, "unproven": 2, "rows": 3}
    assert {r["status"] for r in report["rows"]} <= {"pass", "fail", "unproven"}

    p1 = acc.build_report(cfg, phase="P1", cached=cached)
    assert {r["id"]: r["status"] for r in p1["rows"]}["MVP-07-A3"] == "fail"

    everything = acc.build_report(cfg, phase="all", cached=cached)
    assert len(everything["rows"]) == 6
    # A test tagged with an ID the spec does not have is named, not dropped.
    assert everything["orphans"][0]["id"] == "MVP-99-A1"
    # A repo that is not here is reported, and the report still answers.
    site = next(r for r in everything["repos"] if r["name"] == "site")
    assert site["found"] is False


def test_a_results_file_for_a_js_repo_is_read(tmp_path):
    cfg_path = _workspace(tmp_path)
    (tmp_path / "ui" / "results.json").write_text(json.dumps({"testResults": [{
        "name": str(tmp_path / "ui/src/Rail.test.tsx"),
        "assertionResults": [{"title": "MVP-00-A2 off is absent", "status": "passed"}],
    }]}), encoding="utf-8")
    report = acc.build_report(acc.load_config(cfg_path), phase="P0", cached={})
    assert {r["id"]: r["status"] for r in report["rows"]}["MVP-00-A2"] == "pass"


def test_an_unknown_phase_is_refused(tmp_path):
    with pytest.raises(ValueError, match="phase"):
        acc.build_report(acc.load_config(_workspace(tmp_path)), phase="P9", cached={})


def test_running_the_tagged_python_tests_records_their_results(tmp_path):
    cfg = acc.load_config(_workspace(tmp_path))
    results = acc.run_repo(cfg.repos[0], acc.scan_repo(cfg.repos[0])[0])
    assert results["tests/test_a.py::test_rail"] == "passed"
    assert results["tests/test_a.py::test_denial"] == "passed"


# --------------------------------------------------------------------------
# Surfaces: one skill behind CLI, MCP and chat
# --------------------------------------------------------------------------


def _ctx(tmp_path):
    from axiom.infra.skills import SkillContext, SkillRegistry

    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path,
                        logger=logging.getLogger("t"))


class TestSurfaces:
    def test_the_read_is_on_cli_mcp_and_the_agent_and_the_run_is_cli_only(self):
        from axiom.extensions.builtins.app_buildout.skills import bind
        from axiom.infra.skills import SkillRegistry

        reg = SkillRegistry()
        bind(reg)
        read = reg.spec("app.acceptance")
        assert set(read.surfaces) >= {"cli", "mcp", "agent_tool"}
        assert read.side_effects is False
        assert "how far" in read.description.lower()
        run = reg.spec("app.acceptance_run")
        assert run.surfaces == ("cli",)

    def test_the_skill_reads_the_cache_the_cli_run_wrote(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.app_buildout import skills

        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
        cfg_path = _workspace(tmp_path)
        ran = skills.run_tests({"config": str(cfg_path), "phase": "P0"}, _ctx(tmp_path))
        assert ran.ok, ran.errors
        res = skills.acceptance({"config": str(cfg_path), "phase": "P0"}, _ctx(tmp_path))
        assert res.ok
        rows = {r["id"]: r["status"] for r in res.value["rows"]}
        assert rows["MVP-00-A1"] == "pass"
        assert any("P0:" in line for line in res.actions_taken)

    def test_the_cli_prints_json(self, tmp_path, monkeypatch, capsys):
        from axiom.extensions.builtins.app_buildout import cli

        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
        cfg_path = _workspace(tmp_path)
        rc = cli.main(["acceptance", "--phase", "P0", "--config", str(cfg_path), "--json"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["phase"] == "P0" and out["totals"]["rows"] == 3

    def test_a_missing_spec_says_where_it_looked(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.app_buildout import skills

        cfg = _write(tmp_path, "acceptance.toml", '[acceptance]\nspec = "nope.md"\n')
        res = skills.acceptance({"config": str(cfg)}, _ctx(tmp_path))
        assert not res.ok
        assert "nope.md" in res.errors[0]
