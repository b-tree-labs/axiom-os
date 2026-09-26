# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""P0.5 — the principal a harness works for need not be a person.

``vega.identity.Principal`` already admits "human, agent, node, org", so these
tests pin the consequences of recording that rather than widening anything:
provisioning splits by kind, and verification for a non-human principal is a
machine round trip the platform drives at both ends.
"""

from __future__ import annotations

import pytest

from ..models import ContactEndpoint, PrincipalKind, PrincipalProfile, PrincipalStatus
from ..provision import ProvisionError, provision
from ..verify import MachineRoundTrip, verify_endpoints_machine


def _service_declaration(**over):
    base = {
        "handle": "@room-scheduler:site",
        "display_name": "Room Scheduler",
        "kind": "service",
        "endpoints": [{"kind": "email", "address": "rooms@example.invalid"}],
        "preferences": [{"topic_class": "*", "ranked_kinds": ["email"]}],
    }
    base.update(over)
    return base


class _FakeRoundTrip:
    """Drives both ends the way a real mailbox probe does."""

    def __init__(self, *, read_back=True, delete_ok=True, still_there_after_delete=False):
        self.store: dict[str, str] = {}
        self._read_back = read_back
        self._delete_ok = delete_ok
        self._still_there = still_there_after_delete
        self.calls: list[str] = []

    def write(self, endpoint, payload):
        self.calls.append("write")
        self.store[endpoint.address] = payload
        return f"artifact-{endpoint.kind}"

    def read(self, endpoint, artifact_id):
        self.calls.append("read")
        if not self._read_back:
            return False
        if "delete" in self.calls:
            return self._still_there
        return endpoint.address in self.store

    def delete(self, endpoint, artifact_id):
        self.calls.append("delete")
        if not self._delete_ok:
            raise RuntimeError("mailbox refused the cleanup")
        self.store.pop(endpoint.address, None)


# --- provisioning splits by kind ---------------------------------------------


def test_service_principal_provisions_without_any_prompt():
    principal = provision(_service_declaration())
    assert principal.kind is PrincipalKind.SERVICE
    assert principal.requires_interview is False
    assert principal.endpoint("email").address == "rooms@example.invalid"


def test_service_principal_does_not_carry_a_style_card():
    # Voice is a property of a person. A service identity has a service persona,
    # which is declared, not learned, and carries no consent story at all.
    assert provision(_service_declaration()).supports_voice is False
    assert PrincipalProfile(handle="@ben:netl", display_name="B").supports_voice is True


def test_human_principals_are_not_provisionable_declaratively():
    # The interview is the human path. Letting a human be declared with defaults
    # filled in produces a record that *looks* interviewed and was not, which is
    # the "configured therefore fine" failure this extension exists to prevent.
    with pytest.raises(ProvisionError, match="interview"):
        provision(_service_declaration(kind="human"))


def test_provision_rejects_an_unknown_kind():
    with pytest.raises(ProvisionError, match="unknown principal kind"):
        provision(_service_declaration(kind="chatbot"))


def test_provision_requires_at_least_one_endpoint():
    with pytest.raises(ProvisionError, match="at least one endpoint"):
        provision(_service_declaration(endpoints=[]))


def test_provision_validates_the_handle_through_the_identity_layer():
    with pytest.raises(ProvisionError, match="handle"):
        provision(_service_declaration(handle="ben@netl"))


# --- machine round trip -------------------------------------------------------


def test_machine_round_trip_verifies_and_leaves_nothing_behind():
    endpoints = [ContactEndpoint(kind="email", address="rooms@example.invalid")]
    probe = _FakeRoundTrip()

    outcome = verify_endpoints_machine(endpoints, roundtrip=probe)

    assert outcome.verified == ["email"]
    assert endpoints[0].is_verified
    # write -> read -> delete -> read: the final read is what proves cleanup,
    # and asserting on it is the difference between "we deleted it" and "it is gone".
    assert probe.calls == ["write", "read", "delete", "read"]
    assert probe.store == {}


def test_round_trip_that_cannot_read_back_is_not_verified():
    endpoints = [ContactEndpoint(kind="email", address="rooms@example.invalid")]
    outcome = verify_endpoints_machine(endpoints, roundtrip=_FakeRoundTrip(read_back=False))

    assert outcome.verified == []
    assert outcome.degraded == ["email"]
    assert endpoints[0].verified_at is None


def test_failed_cleanup_is_a_verification_failure():
    # An unverified probe that litters someone's mailbox gets switched off by
    # whoever owns that mailbox, so leaving an artifact behind fails the check.
    endpoints = [ContactEndpoint(kind="email", address="rooms@example.invalid")]
    outcome = verify_endpoints_machine(endpoints, roundtrip=_FakeRoundTrip(delete_ok=False))

    assert outcome.degraded == ["email"]
    assert "cleanup" in (endpoints[0].health_reason or "")


def test_artifact_surviving_deletion_fails_even_though_delete_returned():
    endpoints = [ContactEndpoint(kind="email", address="rooms@example.invalid")]
    probe = _FakeRoundTrip(still_there_after_delete=True)

    outcome = verify_endpoints_machine(endpoints, roundtrip=probe)

    assert outcome.degraded == ["email"]
    assert "still present" in (endpoints[0].health_reason or "")


def test_machine_round_trip_is_a_protocol_a_real_probe_can_satisfy():
    assert isinstance(_FakeRoundTrip(), MachineRoundTrip)


# --- the end state P0.5 promises ---------------------------------------------


def test_service_principal_reaches_active_with_no_person_in_the_loop():
    principal = provision(_service_declaration())
    assert principal.status is PrincipalStatus.UNVERIFIED

    verify_endpoints_machine(principal.endpoints, roundtrip=_FakeRoundTrip())
    assert principal.refresh_status() is PrincipalStatus.ACTIVE


# --- persistence --------------------------------------------------------------


def test_kind_survives_a_save_load_round_trip(tmp_path):
    from ..store import load, save

    save(provision(_service_declaration()), config_dir=tmp_path)
    reloaded = load(config_dir=tmp_path)

    assert reloaded.kind is PrincipalKind.SERVICE
    assert reloaded.requires_interview is False


def test_an_existing_record_without_a_kind_reads_as_human(tmp_path):
    # Records written by P0 predate the field. They described people, so the
    # backfill is `human` -- and it must be the *written* default too, so the
    # next save stops relying on the fallback.
    from ..store import load, record_path, save

    save(provision(_service_declaration()), config_dir=tmp_path)
    path = record_path(tmp_path)
    path.write_text(path.read_text().replace('kind = "service"\n', ""))

    assert load(config_dir=tmp_path).kind is PrincipalKind.HUMAN


def test_an_unreadable_kind_refuses_rather_than_defaulting_to_human(tmp_path):
    # Quietly reading a corrupt kind as `human` would route a service account
    # into the interview and the voice model, which is the more consequential
    # branch. Fail closed instead.
    from ..store import load, record_path, save

    save(provision(_service_declaration()), config_dir=tmp_path)
    path = record_path(tmp_path)
    path.write_text(path.read_text().replace('kind = "service"', 'kind = "sentient"'))

    with pytest.raises(ValueError, match="unknown principal kind"):
        load(config_dir=tmp_path)


# --- the skill layer (what MCP, CLI and chat all drive) -----------------------


def _run_setup(tmp_path, probe, **over):
    from ..skills import setup as setup_skill

    params = {
        "config_dir": tmp_path,
        "declaration": _service_declaration(**over),
        "roundtrip": probe,
    }
    return setup_skill.run(params, None)


def test_setup_declares_and_proves_a_service_principal_end_to_end(tmp_path):
    from ..store import load

    result = _run_setup(tmp_path, _FakeRoundTrip())

    assert result.ok
    assert result.value["kind"] == "service"
    assert result.value["verified_endpoints"] == ["email"]
    assert load(config_dir=tmp_path).status is PrincipalStatus.ACTIVE
    # No question was ever returned, so nothing waited on a person.
    assert "next_question" not in result.value


def test_setup_reports_a_bad_declaration_without_writing_a_record(tmp_path):
    from ..store import load

    result = _run_setup(tmp_path, _FakeRoundTrip(), endpoints=[])

    assert not result.ok
    assert "at least one endpoint" in result.errors[0]
    assert load(config_dir=tmp_path) is None


def test_verify_refuses_to_claim_anything_without_a_probe(tmp_path):
    from ..skills import verify as verify_skill

    _run_setup(tmp_path, _FakeRoundTrip(), )
    result = verify_skill.run({"config_dir": tmp_path}, None)

    assert not result.ok
    assert "machine round trip" in result.errors[0]
    assert "nothing is claimed" in result.errors[0]


def test_verify_reruns_as_a_liveness_probe(tmp_path):
    # A machine round trip is repeatable, so `verify` is a health check for a
    # service principal rather than a one-time setup ceremony.
    from ..skills import verify as verify_skill

    _run_setup(tmp_path, _FakeRoundTrip())
    again = verify_skill.run({"config_dir": tmp_path, "roundtrip": _FakeRoundTrip()}, None)

    assert again.ok
    assert again.value["verified"] == ["email"]


def test_a_failing_probe_degrades_without_erasing_proof(tmp_path):
    # The blip rule holds on the machine path too: a probe failure records
    # `degraded` but must not clear `verified_at`, or one bad minute would
    # permanently remove a principal's only route. Routing skips `failed`, not
    # `degraded`, so a channel that has worked stays a candidate.
    from ..skills import verify as verify_skill
    from ..store import load

    _run_setup(tmp_path, _FakeRoundTrip())
    proven_at = load(config_dir=tmp_path).endpoint("email").verified_at

    broken = verify_skill.run(
        {"config_dir": tmp_path, "roundtrip": _FakeRoundTrip(read_back=False)}, None
    )

    assert not broken.ok
    assert broken.value["degraded"] == ["email"]

    after = load(config_dir=tmp_path).endpoint("email")
    assert after.verified_at == proven_at
    assert after.health.value == "degraded"
