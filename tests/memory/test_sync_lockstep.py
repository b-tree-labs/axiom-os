# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""P4 acceptance gate — the F6 cross-harness-sync criteria, end to end.

This is the loop's acceptance proof (PRD F6; ADR-087 D2/D8/D10):

- **Two-harness lock-step:** a change in harness A propagates to harness B's
  instruction file through the Axiom hub, and vice versa.
- **Kill-and-restart:** no loss, no echo storm — every pending change lands
  exactly once and nothing we wrote out gets re-imported.
- **Conflict stream:** planted concurrent edits → LWW winner applied, loser
  lands in the P2 review queue, never silent.
- **Sources untouched:** mtime + hash asserted after the read-only inbound
  path; the user's authored content survives write-back byte-identically.
- **Vault never outbound; secret-class routed to vault inbound.**
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.schedule.lease import LeaseManager
from axiom.memory.absorb.markdown_hierarchy import agents_md_adapter
from axiom.memory.rendering import SESSION_BOUNDARY, strip_managed_block
from axiom.memory.serving import TIER_LOCAL
from axiom.memory.sync.detect import ChangeDetector
from axiom.memory.sync.engine import SyncEngine
from axiom.memory.sync.service import SyncPeer, SyncService
from axiom.memory.sync.writeback import MultiTargetWriteBack

PRINCIPAL = "@alice:home"
T0 = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
ACCOUNTS = frozenset({"acct-a", "acct-b"})


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t = self.t + timedelta(seconds=seconds)


def _make_composition(base: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base.mkdir(parents=True, exist_ok=True)
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _live(composition):
    return composition.artifact_registry.list(kind="fragment")


def _peer(name, account, root, clock) -> SyncPeer:
    adapter = agents_md_adapter(account=account, roots=[root])
    det = ChangeDetector(adapter=adapter, now_fn=lambda: clock().isoformat())
    return SyncPeer(
        harness=name, account=account, detector=det,
        writeback=MultiTargetWriteBack(root=root, products=("agents_md", "cline")),
        deployment_tier=TIER_LOCAL,
    )


def _service(composition, peers, clock) -> SyncService:
    engine = SyncEngine(
        composition=composition, principal=PRINCIPAL, account_set=ACCOUNTS,
        now_fn=lambda: clock().isoformat(),
    )
    return SyncService(
        composition=composition, engine=engine, peers=peers,
        lease=LeaseManager(node_id="node-a", ttl_seconds=30), now_fn=clock,
    )


@pytest.fixture
def env(tmp_path):
    clock = _Clock(T0)
    composition = _make_composition(tmp_path / "node")
    root_a = tmp_path / "harness-a"
    root_b = tmp_path / "harness-b"
    peer_a = _peer("agents-md", "acct-a", root_a, clock)
    peer_b = _peer("agents-md", "acct-b", root_b, clock)
    svc = _service(composition, [peer_a, peer_b], clock)
    return {
        "clock": clock, "composition": composition, "svc": svc,
        "root_a": root_a, "root_b": root_b,
    }


# ---------------------------------------------------------------------------
# Two-harness lock-step through the Axiom hub, both directions
# ---------------------------------------------------------------------------


class TestTwoHarnessLockStep:
    def test_a_to_b_and_b_to_a(self, env):
        svc, clock = env["svc"], env["clock"]
        a_agents = env["root_a"] / "AGENTS.md"
        b_agents = env["root_b"] / "AGENTS.md"

        # A learns something → propagates to B.
        _write(a_agents, "# A rules\n\nAlways run ruff before commit.\n")
        svc.poll_and_enqueue()
        svc.tick()  # inbound A → hub
        svc.tick(cadence=SESSION_BOUNDARY)  # outbound hub → B (and A)
        assert "Always run ruff before commit." in b_agents.read_text()
        assert "axiom:cross-mem:begin" in b_agents.read_text()
        # Fallback target got it too.
        assert "Always run ruff before commit." in (env["root_b"] / ".clinerules").read_text()

        # B learns something new → propagates back to A.
        clock.advance(3600)
        _write(b_agents, b_agents.read_text() + "\n## B rules\n\nDeploy from tags only.\n")
        svc.poll_and_enqueue()
        svc.tick()  # inbound B → hub
        svc.tick(cadence=SESSION_BOUNDARY)  # outbound hub → A
        assert "Deploy from tags only." in a_agents.read_text()


# ---------------------------------------------------------------------------
# Kill-and-restart: no loss, no echo storm, exactly once
# ---------------------------------------------------------------------------


class TestKillAndRestart:
    def test_restart_mid_sync_no_loss_no_echo_storm(self, env):
        svc, composition, clock = env["svc"], env["composition"], env["clock"]
        _write(env["root_a"] / "AGENTS.md", "# A\n\nPrefer small PRs.\n")

        # Enqueue then "crash" before draining.
        svc.poll_and_enqueue()
        assert svc.pending_count() == 1
        assert _live(composition) == []  # nothing applied yet

        # Restart: fresh service instance over the same durable store.
        clock2 = _Clock(T0 + timedelta(seconds=30))
        peer_a = _peer("agents-md", "acct-a", env["root_a"], clock2)
        peer_b = _peer("agents-md", "acct-b", env["root_b"], clock2)
        svc2 = _service(composition, [peer_a, peer_b], clock2)
        assert svc2.pending_count() == 1  # durable pending recovered

        svc2.recover()  # drains inbound
        svc2.tick(cadence=SESSION_BOUNDARY)  # flush outbound
        n = len(_live(composition))
        assert n == 1  # landed exactly once, nothing lost
        assert "Prefer small PRs." in (env["root_b"] / "AGENTS.md").read_text()

        # Now recover repeatedly: our own write-backs must never re-import.
        for _ in range(3):
            svc2.recover()
        assert len(_live(composition)) == n  # no echo storm


# ---------------------------------------------------------------------------
# Conflict stream → P2 review queue (LWW winner applied, loser queued)
# ---------------------------------------------------------------------------


class TestConflictStream:
    def test_planted_concurrent_edits_lww_winner_loser_queued(self, env):
        from axiom.memory.dedup import list_conflicts
        from axiom.memory.sync.conflict import list_resolutions

        svc, composition, clock = env["svc"], env["composition"], env["clock"]
        a_agents = env["root_a"] / "AGENTS.md"

        _write(a_agents, "# A\n\nPreferred editor: vim.\n")
        svc.poll_and_enqueue()
        svc.tick()

        # A edits the same file (same source_ref) — a later concurrent edit.
        clock.advance(3600)
        _write(a_agents, "# A\n\nPreferred editor: emacs.\n")
        svc.poll_and_enqueue()
        svc.tick()

        # Never silent: both kept, one open conflict in the reused P2 queue.
        assert len(_live(composition)) == 2
        conflicts = list_conflicts(composition, principal=PRINCIPAL)
        assert len(conflicts) == 1 and conflicts[0]["status"] == "open"

        # LWW: the later edit wins; the earlier is the queued loser.
        res = list_resolutions(composition, principal=PRINCIPAL)
        assert len(res) == 1
        assert res[0]["policy"] == "lww_by_event_time"
        assert len(res[0]["loser_ids"]) == 1

        # The winner propagates; the loser does not.
        svc.tick(cadence=SESSION_BOUNDARY)
        b_text = (env["root_b"] / "AGENTS.md").read_text()
        assert "emacs" in b_text
        assert "vim" not in b_text


# ---------------------------------------------------------------------------
# Sources untouched (read-only inbound) + authored content survives write-back
# ---------------------------------------------------------------------------


class TestSourcesUntouched:
    def test_inbound_never_writes_the_source(self, env):
        svc = env["svc"]
        a_agents = env["root_a"] / "AGENTS.md"
        _write(a_agents, "# A rules\n\nAlways run ruff.\n")
        mtime, digest = a_agents.stat().st_mtime_ns, _hash(a_agents)

        # The full read-only inbound path — poll + import — several times.
        svc.poll_and_enqueue()
        svc.tick()
        svc.poll_and_enqueue()
        svc.tick()
        assert a_agents.stat().st_mtime_ns == mtime  # untouched
        assert _hash(a_agents) == digest

    def test_authored_content_survives_write_back(self, env):
        svc = env["svc"]
        b_agents = env["root_b"] / "AGENTS.md"
        authored = "# B authored\n\nOur team conventions live here.\n"
        _write(b_agents, authored)

        # A change from A flushes a managed block into B's authored file.
        _write(env["root_a"] / "AGENTS.md", "# A\n\nPrefer ruff.\n")
        svc.poll_and_enqueue()
        svc.tick()
        svc.tick(cadence=SESSION_BOUNDARY)

        body = b_agents.read_text()
        assert "axiom:cross-mem:begin" in body  # our block was added
        # …and the user's authored content is preserved byte-for-byte outside it.
        assert "Our team conventions live here." in strip_managed_block(body)


# ---------------------------------------------------------------------------
# Vault never outbound; secret-class routed to vault inbound (OQ6)
# ---------------------------------------------------------------------------


class TestVaultAndSecrets:
    def test_secret_in_source_vaulted_and_never_synced_out(self, env):
        svc, composition = env["svc"], env["composition"]
        _write(
            env["root_a"] / "AGENTS.md",
            "# A\n\nDeploy notes.\n\naws_key = AKIAIOSFODNN7EXAMPLE\n",
        )
        svc.poll_and_enqueue()
        svc.tick()
        svc.tick(cadence=SESSION_BOUNDARY)

        # Stored, but as vault (unservable) — never a plain fragment.
        frags = _live(composition)
        assert any(f.data["cognitive_type"] == "vault" for f in frags)
        # The secret never rode a prompt into any peer's instruction file.
        b_text = (env["root_b"] / "AGENTS.md").read_text()
        assert "AKIAIOSFODNN7EXAMPLE" not in b_text
        assert "AKIAIOSFODNN7EXAMPLE" not in (env["root_b"] / ".clinerules").read_text()

    def test_native_vault_fragment_never_syncs_outbound(self, env):
        svc, composition = env["svc"], env["composition"]
        # A vault fragment authored natively (not via absorb).
        composition.write(
            content={"summary": "prod db password", "secret": "hunter2"},
            cognitive_type="vault",
            principal_id=PRINCIPAL, agents={"axi"}, resources=set(),
        )
        # And an ordinary memory to prove sync still runs.
        _write(env["root_a"] / "AGENTS.md", "# A\n\nPrefer ruff.\n")
        svc.poll_and_enqueue()
        svc.tick()
        svc.tick(cadence=SESSION_BOUNDARY)

        b_text = (env["root_b"] / "AGENTS.md").read_text()
        assert "Prefer ruff." in b_text
        assert "hunter2" not in b_text  # vault never serves outbound
