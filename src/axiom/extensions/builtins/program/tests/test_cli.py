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
        for verb in ("status", "render", "validate", "sync", "changes"):
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


class TestSyncAndChanges:
    """``sync`` and ``changes`` through ``main()`` against the node state dir
    the autouse fixture isolates to the test tree."""

    @pytest.fixture
    def seeded_state(self, tmp_path, data_dict):
        import json

        state = tmp_path / "axi-state"
        (state / "program").mkdir(parents=True)
        (state / "program" / "data.json").write_text(
            json.dumps(data_dict, indent=1), encoding="utf-8"
        )
        return state

    def test_sync_reaches_the_skill_and_logs_the_baseline(self, seeded_state, capsys):
        exit_code = cli.main(["sync", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["value"]["count"] == 9

    def test_changes_reaches_the_skill(self, seeded_state, capsys):
        assert cli.main(["sync"]) == 0
        capsys.readouterr()
        exit_code = cli.main(["changes", "--principal", "@casey:example-org", "--peek", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["value"]["count"] == 9
        assert payload["value"]["advanced"] is False

    def test_cli_changes_default_advances(self, seeded_state, capsys):
        assert cli.main(["sync"]) == 0
        capsys.readouterr()
        # default (no --peek) advances on the CLI surface
        assert cli.main(["changes", "--principal", "@casey:example-org", "--json"]) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["value"]["advanced"] is True
        # a second default read now sees nothing new
        assert cli.main(["changes", "--principal", "@casey:example-org", "--json"]) == 0
        second = json.loads(capsys.readouterr().out)
        assert second["value"]["count"] == 0


class TestMutationVerbs:
    """The nested mutation nouns dispatch through ``main()`` to the right
    capability and edit the node's own data file."""

    @pytest.fixture
    def seeded(self, tmp_path, data_dict):
        state = tmp_path / "axi-state"
        (state / "program").mkdir(parents=True)
        (state / "program" / "data.json").write_text(
            json.dumps(data_dict, indent=1), encoding="utf-8"
        )
        return state

    def _data(self, seeded) -> dict:
        return json.loads((seeded / "program" / "data.json").read_text())

    def test_person_add_edits_the_file(self, seeded, capsys):
        code = cli.main(
            ["person", "add", "--principal", "@newbie:example-org", "--lane", "alpha", "--json"]
        )
        assert code == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True
        assert any(p["principal"] == "@newbie:example-org" for p in self._data(seeded)["people"])

    def test_lane_add_then_ownership_reads_back(self, seeded, capsys):
        assert (
            cli.main(
                ["lane", "add", "--id", "gamma", "--name", "Gamma", "--lead", "@casey:example-org", "--json"]
            )
            == 0
        )
        capsys.readouterr()
        assert cli.main(["ownership", "--scope", "lane", "--key", "gamma", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["value"]["current"] == "@casey:example-org"

    def test_item_reassign(self, seeded, capsys):
        assert cli.main(["item", "reassign", "--id", "i-one", "--owner", "@dana:example-org", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True
        assert self._data(seeded)["schedule"][0]["owner"] == "@dana:example-org"

    def test_invite_and_redeem_round_trip(self, seeded, capsys):
        assert (
            cli.main(["invite", "--principal", "@guest:example-org", "--lane", "alpha", "--role", "guest", "--json"])
            == 0
        )
        code = json.loads(capsys.readouterr().out)["value"]["code"]
        assert cli.main(["redeem", "--code", code, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True
        assert any(p["principal"] == "@guest:example-org" for p in self._data(seeded)["people"])

    def test_an_unknown_subverb_is_a_parse_error(self):
        with pytest.raises(SystemExit):
            cli.main(["person", "frobnicate"])
