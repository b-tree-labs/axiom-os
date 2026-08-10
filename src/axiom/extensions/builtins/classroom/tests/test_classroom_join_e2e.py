# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end classroom-join ceremony tests.

Tier A PR 5 — exercises the full student↔coordinator round trip
through an injectable HTTP transport so neither a live server nor a
docker-compose harness is required to land the integration. A thin
adapter over ``http.server`` can reuse this exact transport interface
in a follow-up without changing any ceremony logic.

Flow covered:
    student                                  coordinator
    -------                                  -----------
    ClassroomJoinClient.join()
      ├─ sign_join_request
      ├─ encode_join_request
      ├─ transport.post(url, body)  ─▶  coordinator_join_endpoint(body)
      │                                  ├─ process_join_request
      │                                  └─ encode_membership_manifest
      ◀─ response body (HTTP 200 / 4xx)
      ├─ decode_membership_manifest
      ├─ verify_membership_manifest
      └─ MembershipStore.save
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.classroom_client import (
    ClassroomJoinClient,
    JoinClientError,
)
from axiom.extensions.builtins.classroom.classroom_coordinator import (
    InMemoryInviteRegistry,
    coordinator_join_endpoint,
)
from axiom.extensions.builtins.classroom.classroom_federation import create_cohort
from axiom.extensions.builtins.classroom.invite_token import (
    create_invite_token,
    encode_invite,
)
from axiom.extensions.builtins.classroom.student_membership import (
    MembershipStore,
)
from axiom.vega.federation.identity import generate_identity

# ---------------------------------------------------------------------------
# In-process "HTTP" transport for tests
# ---------------------------------------------------------------------------


class InProcessTransport:
    """Pretends to be an HTTP client; actually calls the coordinator endpoint directly.

    The real adapter (not in this PR) hits a live HTTP server. This
    fake makes end-to-end tests hermetic without needing one.
    """

    def __init__(self, endpoint):
        """``endpoint`` is a callable ``(body: str) -> (status: int, body: str)``."""
        self._endpoint = endpoint
        self.last_url = None
        self.last_body = None

    def post(self, url: str, body: str) -> tuple[int, str]:
        self.last_url = url
        self.last_body = body
        return self._endpoint(body)


# ---------------------------------------------------------------------------
# Fixtures — full student + coordinator setup
# ---------------------------------------------------------------------------


@pytest.fixture
def coordinator_identity(tmp_path):
    return generate_identity(
        owner="ondrej@ctu.cz",
        display_name="Test Coordinator",
        keys_dir=tmp_path / "coord-keys",
    )


@pytest.fixture
def student_identity(tmp_path):
    return generate_identity(
        owner="alice@example.org",
        display_name="Alice",
        keys_dir=tmp_path / "student-keys",
    )


@pytest.fixture
def cohort_state(coordinator_identity):
    """Mutable wrapper around (cohort, invite_registry) so the coordinator
    endpoint can update it between requests."""
    return {
        "cohort": create_cohort("ne101-prague-2026", coordinator_identity.node_id),
        "registry": InMemoryInviteRegistry(),
    }


@pytest.fixture
def coordinator_endpoint(coordinator_identity, cohort_state):
    def _endpoint(body: str) -> tuple[int, str]:
        status, response_body, updated_cohort = coordinator_join_endpoint(
            encoded_request=body,
            coordinator_identity=coordinator_identity,
            cohort=cohort_state["cohort"],
            invite_registry=cohort_state["registry"],
        )
        if updated_cohort is not None:
            cohort_state["cohort"] = updated_cohort
        return status, response_body

    return _endpoint


@pytest.fixture
def transport(coordinator_endpoint):
    return InProcessTransport(endpoint=coordinator_endpoint)


@pytest.fixture
def student_store(tmp_path):
    return MembershipStore(base_dir=tmp_path / "alice-state")


@pytest.fixture
def issued_invite_encoded(coordinator_identity, cohort_state):
    invite = create_invite_token(
        classroom_id="ne101-prague-2026",
        coordinator_id=coordinator_identity.node_id,
        ttl_hours=24,
    )
    cohort_state["registry"].register(invite)
    return encode_invite(invite), invite


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestJoinHappyPath:
    def test_student_joins_successfully_and_persists_manifest(
        self,
        student_identity,
        coordinator_identity,
        transport,
        student_store,
        issued_invite_encoded,
        cohort_state,
    ):
        encoded_invite, _ = issued_invite_encoded
        client = ClassroomJoinClient(
            student_identity=student_identity,
            transport=transport,
            store=student_store,
        )
        result = client.join(
            encoded_invite=encoded_invite,
            student_id="alice",
            coordinator_url="https://test-coordinator.example/classroom/join",
        )
        # Returned membership record is valid and saved.
        assert result.accepted is True
        assert result.membership is not None
        assert result.membership.classroom_id == "ne101-prague-2026"
        assert result.membership.student_id == "alice"
        # Store now has the membership on disk.
        loaded = student_store.load("ne101-prague-2026")
        assert loaded.manifest == result.membership.manifest
        # Coordinator side now shows alice as a cohort member.
        assert len(cohort_state["cohort"].members) == 1
        assert cohort_state["cohort"].members[0].student_id == "alice"

    def test_transport_receives_expected_url_and_body(
        self,
        student_identity,
        coordinator_identity,
        transport,
        student_store,
        issued_invite_encoded,
    ):
        encoded_invite, _ = issued_invite_encoded
        client = ClassroomJoinClient(
            student_identity=student_identity,
            transport=transport,
            store=student_store,
        )
        client.join(
            encoded_invite=encoded_invite,
            student_id="alice",
            coordinator_url="https://test-coordinator.example/classroom/join",
        )
        assert transport.last_url == "https://test-coordinator.example/classroom/join"
        assert isinstance(transport.last_body, str)
        assert transport.last_body  # non-empty encoded request


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestJoinFailures:
    def test_malformed_invite_raises_before_transport(
        self,
        student_identity,
        transport,
        student_store,
    ):
        client = ClassroomJoinClient(
            student_identity=student_identity,
            transport=transport,
            store=student_store,
        )
        with pytest.raises(JoinClientError):
            client.join(
                encoded_invite="not-a-valid-invite",
                student_id="alice",
                coordinator_url="https://test-coordinator.example/classroom/join",
            )
        # Transport should NOT have been called.
        assert transport.last_url is None

    def test_coordinator_rejects_unknown_invite(
        self,
        student_identity,
        coordinator_identity,
        transport,
        student_store,
    ):
        # Create an invite but DON'T register it with the coordinator.
        invite = create_invite_token(
            classroom_id="ne101-prague-2026",
            coordinator_id=coordinator_identity.node_id,
            ttl_hours=24,
        )
        encoded = encode_invite(invite)

        client = ClassroomJoinClient(
            student_identity=student_identity,
            transport=transport,
            store=student_store,
        )
        result = client.join(
            encoded_invite=encoded,
            student_id="alice",
            coordinator_url="https://test-coordinator.example/classroom/join",
        )
        assert result.accepted is False
        assert result.membership is None
        assert "invite" in result.error.lower()
        # Nothing persisted locally.
        assert student_store.list_ids() == []

    def test_coordinator_error_response_does_not_persist_manifest(
        self,
        student_identity,
        coordinator_identity,
        transport,
        student_store,
        cohort_state,
    ):
        # Expired invite → coordinator returns 4xx; client preserves error
        # and does not persist anything.
        invite = create_invite_token(
            classroom_id="ne101-prague-2026",
            coordinator_id=coordinator_identity.node_id,
            ttl_hours=0,  # instantly expired
        )
        cohort_state["registry"].register(invite)
        encoded = encode_invite(invite)

        client = ClassroomJoinClient(
            student_identity=student_identity,
            transport=transport,
            store=student_store,
        )
        result = client.join(
            encoded_invite=encoded,
            student_id="alice",
            coordinator_url="https://test-coordinator.example/classroom/join",
        )
        assert result.accepted is False
        assert "expired" in result.error.lower()
        assert student_store.list_ids() == []


# ---------------------------------------------------------------------------
# HTTP-ish response shape of the coordinator endpoint
# ---------------------------------------------------------------------------


class TestCoordinatorEndpoint:
    def test_endpoint_returns_200_on_accepted(
        self, coordinator_identity, cohort_state, student_identity, issued_invite_encoded
    ):
        from axiom.extensions.builtins.classroom.classroom_join_request import (
            encode_join_request,
            sign_join_request,
        )
        from axiom.extensions.builtins.classroom.invite_token import decode_invite

        encoded_invite, _ = issued_invite_encoded
        invite = decode_invite(encoded_invite)
        request = sign_join_request(student_identity, invite, "alice")
        encoded = encode_join_request(request)

        status, body, updated = coordinator_join_endpoint(
            encoded_request=encoded,
            coordinator_identity=coordinator_identity,
            cohort=cohort_state["cohort"],
            invite_registry=cohort_state["registry"],
        )
        assert status == 200
        assert body  # encoded manifest
        assert updated is not None

    def test_endpoint_returns_400_on_rejected(
        self, coordinator_identity, cohort_state
    ):
        status, body, updated = coordinator_join_endpoint(
            encoded_request="garbage",
            coordinator_identity=coordinator_identity,
            cohort=cohort_state["cohort"],
            invite_registry=cohort_state["registry"],
        )
        assert status == 400
        assert body
        assert updated is None
