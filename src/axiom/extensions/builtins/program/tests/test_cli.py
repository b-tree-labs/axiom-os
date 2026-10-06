# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi program`` — argparse wrappers over the program.* skills.

Per ADR-056 the handlers hold zero logic: each verb translates flags into a
params dict and dispatches through ``invoke_capability``. These tests go
through ``main()`` — the door a person uses — and additionally pin the
registry bindings so a declared verb cannot silently be a capability that
does not exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.program import cli, skills


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """CLI invocations write audit state; keep it inside the test tree."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "axi-state"))


class TestRegistryBindings:
    def test_every_cli_verb_is_a_registered_capability(self):
        registry = skills.bind_default()
        for verb in ("status", "render", "validate"):
            name = f"program.{verb}"
            assert registry.has(name), name
            spec = registry.spec(name)
            assert spec is not None and spec.description.strip(), name

    def test_binding_is_idempotent(self):
        first = skills.bind_default()
        second = skills.bind_default()
        assert first.has("program.status") and second.has("program.status")


class TestDispatch:
    def test_status_reaches_the_skill(self, data_file: Path, capsys):
        exit_code = cli.main(["status", "--scope", "schedule", "--data", str(data_file), "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert [i["id"] for i in payload["value"]["items"]] == [
            "i-one",
            "i-two",
            "i-three",
            "i-four",
        ]

    def test_status_scoping_flags_thread_through(self, data_file: Path, capsys):
        exit_code = cli.main(
            [
                "status",
                "--scope",
                "person",
                "--key",
                "@casey:example-org",
                "--data",
                str(data_file),
                "--json",
            ]
        )
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert [i["id"] for i in payload["value"]["items"]] == ["i-one", "i-three"]

    def test_a_refusal_is_exit_one_on_stderr(self, data_file: Path, capsys):
        exit_code = cli.main(
            ["status", "--scope", "lane", "--key", "gamma", "--data", str(data_file)]
        )
        assert exit_code == 1
        assert "gamma" in capsys.readouterr().err

    def test_render_reaches_the_skill(self, data_file: Path, tmp_path: Path, capsys):
        out = tmp_path / "site"
        exit_code = cli.main(["render", "--data", str(data_file), "--out", str(out)])
        assert exit_code == 0
        assert (out / "status.html").exists()

    def test_validate_passes_a_good_file(self, data_file: Path, capsys):
        assert cli.main(["validate", "--data", str(data_file)]) == 0

    def test_validate_fails_a_bad_file_and_names_the_defect(self, write_data, data_dict, capsys):
        data_dict["schedule"][0]["status"] = "done"
        exit_code = cli.main(["validate", "--data", str(write_data(data_dict))])
        assert exit_code == 1
        assert "status" in capsys.readouterr().err

    def test_an_unknown_verb_is_a_parse_error(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["destroy"])
