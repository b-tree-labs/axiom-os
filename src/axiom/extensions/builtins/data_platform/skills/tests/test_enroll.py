# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""``data.enroll`` — the account a producer cannot create for itself."""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.data_platform.agents.plinth.connectors import load_connector
from axiom.extensions.builtins.data_platform.skills.enroll import (
    connector_name,
    issue_command,
    parse_manifest,
    run,
)
from axiom.infra.skills import SkillContext

MEASURED = """
[source]
name = "archive"
site = "example-site"
stream = "loop.instrument"
schema_ref = "example-site/instrument-v1"
source_class = "measured"

[provider]
kind = "csv_long"
"""

MODEL = """
[source]
name = "surrogate"
site = "example-site"
stream = "loop.predicted"
schema_ref = "example-site/predicted-v1"
source_class = "predicted"
model_ref = "example-model@v3"

[provider]
kind = "ws_json"
"""


@pytest.fixture
def ctx(tmp_path):
    return SkillContext(registry=None, state_dir=tmp_path, logger=logging.getLogger("t"))


def _manifest(tmp_path, text, name="source.toml"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


# ------------------------------------------------------------------ parsing


def test_a_modelled_source_must_name_its_model() -> None:
    text = MODEL.replace('model_ref = "example-model@v3"\n', "")
    _, errors = parse_manifest(text)
    assert any("no model_ref is set" in e for e in errors)


def test_a_measured_source_may_not_carry_a_model_ref() -> None:
    _, errors = parse_manifest(MEASURED.replace("[provider]", 'model_ref = "m@1"\n\n[provider]'))
    assert any("a measurement has no model" in e for e in errors)


def test_missing_required_fields_are_all_reported() -> None:
    _, errors = parse_manifest("[source]\nname = 'x'\n")
    assert any("missing site" in e for e in errors)
    assert any("missing schema_ref" in e for e in errors)


def test_not_a_manifest_is_a_clear_error() -> None:
    _, errors = parse_manifest("[something_else]\nx = 1\n")
    assert any("not a source manifest" in e for e in errors)


def test_invalid_toml_is_reported_as_such() -> None:
    _, errors = parse_manifest("[source\n")
    assert any("not valid TOML" in e for e in errors)


# ----------------------------------------------------------------- naming


def test_connector_is_namespaced_by_site() -> None:
    """Two facilities may both call a source 'archive'."""
    assert connector_name({"name": "archive", "site": "a-site"}) == "a-site-archive"


def test_an_already_namespaced_name_is_not_doubled() -> None:
    assert connector_name({"name": "a-site-archive", "site": "a-site"}) == "a-site-archive"


def test_issue_command_binds_the_key_to_the_site() -> None:
    command = issue_command({"site": "a-site"}, "a-site-archive")
    assert "--site a-site" in command
    assert "--principal @svc-a-site:a-site" in command


# ---------------------------------------------------------------- enrolling


def test_enroll_records_the_site_on_the_connector(tmp_path, ctx) -> None:
    """Conformance attributes rows from this, so it is the whole point."""
    result = run(
        {"manifest": _manifest(tmp_path, MEASURED), "bronze_root": str(tmp_path / "b")}, ctx
    )
    assert result.ok
    stored = load_connector("example-site-archive", state_dir=tmp_path)
    assert stored.site == "example-site"
    assert stored.kind == "push"
    assert stored.params["schema_ref"] == "example-site/instrument-v1"


def test_enroll_carries_model_provenance_onto_the_connector(tmp_path, ctx) -> None:
    result = run({"manifest": _manifest(tmp_path, MODEL), "bronze_root": str(tmp_path / "b")}, ctx)
    assert result.ok
    stored = load_connector("example-site-surrogate", state_dir=tmp_path)
    assert stored.params["source_class"] == "predicted"
    assert stored.params["model_ref"] == "example-model@v3"
    assert any("model source" in a for a in result.actions_taken)


def test_enroll_is_idempotent(tmp_path, ctx) -> None:
    params = {"manifest": _manifest(tmp_path, MEASURED), "bronze_root": str(tmp_path / "b")}
    assert run(params, ctx).ok
    second = run(params, ctx)
    assert second.ok
    assert second.value["reenrolled"] is True


def test_connector_names_are_site_scoped_so_two_sites_cannot_collide(tmp_path, ctx) -> None:
    """The structural protection: the site is *in* the connector name, so two
    facilities using the same short source name land in different connectors
    rather than one overwriting the other."""
    first = run(
        {"manifest": _manifest(tmp_path, MEASURED), "bronze_root": str(tmp_path / "b")}, ctx
    )
    moved = MEASURED.replace('site = "example-site"', 'site = "other-site"')
    second = run(
        {"manifest": _manifest(tmp_path, moved, "other.toml"), "bronze_root": str(tmp_path / "b")},
        ctx,
    )
    assert first.ok and second.ok
    assert first.value["connector"] == "example-site-archive"
    assert second.value["connector"] == "other-site-archive"


def test_re_attributing_an_existing_connector_is_refused(tmp_path, ctx) -> None:
    """Defense in depth for connectors created outside enrollment — `axi data
    register` takes no site, so one can exist attributed to another tenant.
    Silently moving data between tenants is the failure worth refusing."""
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    save_connector(
        ConnectorConfig(
            name="example-site-archive",
            kind="push",
            bronze_root=str(tmp_path / "b"),
            site="someone-else",
        ),
        state_dir=tmp_path,
    )
    result = run(
        {"manifest": _manifest(tmp_path, MEASURED), "bronze_root": str(tmp_path / "b")}, ctx
    )
    assert not result.ok
    assert any("already enrolled to site" in e for e in result.errors)


def test_enroll_prints_the_credential_command_rather_than_running_it(tmp_path, ctx) -> None:
    """Issuing a credential is privileged; everything up to it is automatic."""
    result = run(
        {"manifest": _manifest(tmp_path, MEASURED), "bronze_root": str(tmp_path / "b")}, ctx
    )
    assert "axi gate issue api-key" in result.value["issue_command"]
    assert any("run it yourself" in a for a in result.actions_taken)


def test_a_bad_manifest_enrolls_nothing(tmp_path, ctx) -> None:
    bad = MODEL.replace('model_ref = "example-model@v3"\n', "")
    result = run({"manifest": _manifest(tmp_path, bad), "bronze_root": str(tmp_path / "b")}, ctx)
    assert not result.ok
    assert not (tmp_path / "plinth" / "connectors").exists()


def test_missing_manifest_file_is_a_clear_error(tmp_path, ctx) -> None:
    result = run({"manifest": str(tmp_path / "absent.toml"), "bronze_root": "x"}, ctx)
    assert not result.ok
    assert any("cannot read" in e for e in result.errors)
