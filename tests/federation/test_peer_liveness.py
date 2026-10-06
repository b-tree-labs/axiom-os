# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Liveness for a peer, and the difference between reaching one and knowing one.

The registry already separates transport contact ("you can reach them") from
identity binding ("you know who they are cryptographically"). Its own docstring
says so. What was missing is that nothing ever recorded a contact: the field
called ``last_seen`` was written only by registry ceremonies — discover, bind,
state change — so a peer contacted daily but never re-bound showed a timestamp
frozen at whenever it last changed state.

The probe is transport-aware because the peers are. The one real peer in this
workspace is reachable over SSH and its URL is ``ssh://user@host``; handing that
to an HTTP health check reports a live node as dead, which is worse than a stale
timestamp because it is confidently wrong.
"""

from __future__ import annotations

from axiom.vega.federation.discovery import KnownNode, NodeRegistry, NodeState
from axiom.vega.federation.heartbeat import check_peer


def ssh_node(**over) -> KnownNode:
    fields = dict(
        node_id="0e3725bd4ac88669",
        display_name="peer:one",
        url="ssh://someone@somehost",
        transport="ssh",
        state=NodeState.VERIFIED,
        ssh_user="someone",
        ssh_host="somehost",
    )
    fields.update(over)
    return KnownNode(**fields)


class TestTheProbeFollowsTheTransport:
    def test_an_ssh_peer_is_probed_over_ssh(self) -> None:
        calls = []

        def runner(user, host, cmd):
            calls.append((user, host, cmd))
            return 0, "axi 0.45.0\n", ""

        result = check_peer(
            node_id="n1",
            transport="ssh",
            url="ssh://someone@somehost",
            ssh_user="someone",
            ssh_host="somehost",
            ssh_runner=runner,
        )
        assert result["healthy"] is True
        assert calls == [("someone", "somehost", "axi --version")]

    def test_an_ssh_url_is_never_handed_to_the_http_checker(self) -> None:
        # The regression this exists for. `urlopen("ssh://…/health")` raises on
        # an unknown scheme, so a live SSH peer would be reported dead.
        http_calls = []

        def http_checker(node_id, url, timeout=5.0):
            http_calls.append(url)
            raise AssertionError("an ssh peer must not be probed over HTTP")

        check_peer(
            node_id="n1",
            transport="ssh",
            url="ssh://someone@somehost",
            ssh_user="someone",
            ssh_host="somehost",
            ssh_runner=lambda u, h, c: (0, "axi 0.45.0", ""),
            http_checker=http_checker,
        )
        assert http_calls == []

    def test_an_unreachable_ssh_peer_is_unhealthy_and_says_why(self) -> None:
        result = check_peer(
            node_id="n1",
            transport="ssh",
            url="ssh://someone@somehost",
            ssh_user="someone",
            ssh_host="somehost",
            ssh_runner=lambda u, h, c: (255, "", "Connection refused"),
        )
        assert result["healthy"] is False
        assert "Connection refused" in result["error"]

    def test_an_http_peer_uses_the_existing_health_check(self) -> None:
        seen = {}

        def http_checker(node_id, url, timeout=5.0):
            seen["url"] = url
            return {
                "node_id": node_id,
                "healthy": True,
                "latency_ms": 3,
                "details": {},
                "checked_at": "2026-09-06T00:00:00+00:00",
            }

        result = check_peer(
            node_id="n1",
            transport="a2a",
            url="http://peer:9877",
            http_checker=http_checker,
        )
        assert result["healthy"] is True
        assert seen["url"] == "http://peer:9877"

    def test_an_ssh_peer_with_no_runner_fails_clearly_rather_than_crashing(self) -> None:
        result = check_peer(
            node_id="n1",
            transport="ssh",
            url="ssh://someone@somehost",
            ssh_user="",
            ssh_host="",
            ssh_runner=lambda u, h, c: (0, "", ""),
        )
        assert result["healthy"] is False
        assert "ssh" in result["error"].lower()

    def test_every_result_carries_a_checked_at(self) -> None:
        result = check_peer(
            node_id="n1",
            transport="ssh",
            url="ssh://a@b",
            ssh_user="a",
            ssh_host="b",
            ssh_runner=lambda u, h, c: (0, "axi 0.45.0", ""),
        )
        assert result["checked_at"]


class TestContactIsRecordedSeparatelyFromStateChange:
    def registry(self, tmp_path) -> NodeRegistry:
        reg = NodeRegistry(registry_path=tmp_path / "nodes.yaml")
        reg.add(ssh_node())
        return reg

    def test_recording_a_contact_stamps_last_seen(self, tmp_path) -> None:
        reg = self.registry(tmp_path)
        assert reg.get("0e3725bd4ac88669").last_seen == ""
        assert reg.record_contact("0e3725bd4ac88669") is True
        assert reg.get("0e3725bd4ac88669").last_seen != ""

    def test_a_contact_is_persisted_not_just_held_in_memory(self, tmp_path) -> None:
        # The heartbeat's own PeerStatus was in-memory and nothing read it back.
        reg = self.registry(tmp_path)
        reg.record_contact("0e3725bd4ac88669")
        reloaded = NodeRegistry(registry_path=tmp_path / "nodes.yaml")
        assert reloaded.get("0e3725bd4ac88669").last_seen != ""

    def test_a_state_change_does_not_pretend_to_be_a_contact(self, tmp_path) -> None:
        # The whole defect in one assertion.
        reg = self.registry(tmp_path)
        reg.update_state("0e3725bd4ac88669", NodeState.VERIFIED)
        node = reg.get("0e3725bd4ac88669")
        assert node.state_changed_at != "", "a state change records when it happened"
        assert node.last_seen == "", (
            "a state change is not contact; recording it as one is what made a "
            "peer last contacted in April look like it had been seen in April"
        )

    def test_recording_a_contact_for_an_unknown_node_is_false_not_an_error(self, tmp_path) -> None:
        reg = self.registry(tmp_path)
        assert reg.record_contact("nope") is False


class TestLegacyRecordsAreMigratedHonestly:
    def test_an_old_last_seen_is_read_as_the_state_change_it_was(self) -> None:
        # Every existing last_seen on disk was written by a ceremony, so it is
        # a state-change time. Reading it as contact would carry the original
        # lie forward into the new field.
        legacy = {
            "node_id": "n1",
            "display_name": "peer:one",
            "url": "ssh://a@b",
            "transport": "ssh",
            "state": "verified",
            "last_seen": "2026-04-15T16:29:32+00:00",
        }
        node = KnownNode.from_dict(legacy)
        assert node.state_changed_at == "2026-04-15T16:29:32+00:00"
        assert node.last_seen == "", "we have never recorded contact with this peer"

    def test_a_record_that_already_distinguishes_them_is_left_alone(self) -> None:
        both = {
            "node_id": "n1",
            "display_name": "peer:one",
            "url": "ssh://a@b",
            "transport": "ssh",
            "state": "verified",
            "last_seen": "2026-09-06T10:00:00+00:00",
            "state_changed_at": "2026-04-15T16:29:32+00:00",
        }
        node = KnownNode.from_dict(both)
        assert node.last_seen == "2026-09-06T10:00:00+00:00"
        assert node.state_changed_at == "2026-04-15T16:29:32+00:00"

    def test_both_fields_survive_a_round_trip(self) -> None:
        node = ssh_node(
            last_seen="2026-09-06T10:00:00+00:00", state_changed_at="2026-04-15T16:29:32+00:00"
        )
        again = KnownNode.from_dict(node.to_dict())
        assert again.last_seen == node.last_seen
        assert again.state_changed_at == node.state_changed_at


class TestTheSurfaceStopsOverclaiming:
    """What the operator reads has to answer the question they asked."""

    def test_a_probe_records_contact_for_healthy_peers_only(self, tmp_path) -> None:
        from axiom.extensions.builtins.federation.cli import _probe_peers

        reg = NodeRegistry(registry_path=tmp_path / "nodes.yaml")
        reg.add(ssh_node(node_id="up1", display_name="up"))
        reg.add(ssh_node(node_id="down1", display_name="down"))

        import axiom.vega.federation.heartbeat as hb

        def fake_check_peer(*, node_id, **kw):
            healthy = node_id == "up1"
            return {
                "node_id": node_id,
                "healthy": healthy,
                "latency_ms": 5,
                "checked_at": "2026-09-06T12:00:00+00:00",
                "error": "" if healthy else "refused",
            }

        original = hb.check_peer
        hb.check_peer = fake_check_peer
        try:
            _probe_peers(reg, reg.list_all())
        finally:
            hb.check_peer = original

        assert reg.get("up1").last_seen == "2026-09-06T12:00:00+00:00"
        assert reg.get("down1").last_seen == "", (
            "a failed probe is not contact; recording it would reintroduce the bug"
        )

    def test_contact_column_distinguishes_never_probed_from_a_timestamp(self) -> None:
        from axiom.extensions.builtins.federation.cli import _contact_column

        assert _contact_column(ssh_node(), None) == "never probed"
        # The column now reports an AGE rather than a raw timestamp — nineteen
        # columns of ISO-8601 squeezed the name column into an ellipsis, and
        # the reader had to subtract dates to get the fact they wanted. What
        # matters here is unchanged: a recorded contact reads as a contact,
        # and is not confusable with never having reached the peer.
        recorded = _contact_column(ssh_node(last_seen="2026-09-06T12:00:00+00:00"), None)
        assert recorded != "never probed"
        assert recorded.endswith("ago"), recorded

    def test_contact_column_reports_an_unreachable_probe_as_unreachable(self) -> None:
        from axiom.extensions.builtins.federation.cli import _contact_column

        # Crucially NOT the stored timestamp: a peer that answered in June and
        # is refusing now is down, and showing June would say the opposite.
        col = _contact_column(
            ssh_node(last_seen="2026-06-01T00:00:00+00:00"),
            {"healthy": False, "error": "refused"},
        )
        # Lower case now: the row carries a ❌ beside it, so the word no longer
        # has to shout to be noticed, and every other value in the column is
        # lower case. The assertion that matters is the one in the comment
        # above — it is not the stored June timestamp.
        assert col == "unreachable"
        assert "2026-06" not in col

    def test_a_live_probe_shows_latency_not_a_stored_time(self) -> None:
        from axiom.extensions.builtins.federation.cli import _contact_column

        col = _contact_column(ssh_node(), {"healthy": True, "latency_ms": 884})
        assert "884" in col
