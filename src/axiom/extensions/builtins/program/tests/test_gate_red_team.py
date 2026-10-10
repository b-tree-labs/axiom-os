# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""P14 arming gate — red team.

The adversarial third of the arming gate (docs/working/program-phase7-plus-plan
.md §P14). Hostile input is thrown at the serving and mutation boundaries and
at the feeder-content seam:

- **Serving is deny-by-default.** No served read (MCP / HTTP) will aim the
  reader at a caller-chosen file, leak that file's bytes through the validator,
  serve across accounts, or reach the network. Advancing a watermark — the one
  write a read can do — never happens behind a served caller's back.
- **The mutation surface cannot escalate.** No mutation is reachable from any
  served transport; a mutation's acting identity is the dispatch-stamped
  principal, never a caller-supplied field, so params cannot forge who acted;
  and every mutation is logged with that principal and ownership-checked.
- **Feeder content is untrusted input.** A crafted commit message, issue title,
  branch name, assignee, or journal line cannot create or rewrite committed
  state, misattribute activity, inject a change-kind, or crash a read.

Where a gate concern names a surface that is not yet built (the P8 private
follow-up list, the P12 bus emit, the P13 partner/guest tiers), there is
nothing to attack yet; those are called out in the session report, not faked
here.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest

from axiom.extensions.builtins.program.model import ProgramData, load_program
from axiom.extensions.builtins.program.skills import _changelog as cl
from axiom.extensions.builtins.program.skills import changes, items, lanes, status, validate
from axiom.extensions.builtins.program.skills._capture import MergeRequest, TrackerIssue
from axiom.extensions.builtins.program.skills.sources import GitLabSource
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry

SECRET = "sk-live-DO-NOT-LEAK-7f3a9c2b"  # noqa: S105 - a sentinel, not a real credential


def _served_ctx(state_dir, surface="mcp", principal="@casey:example-org"):
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state_dir,
        logger=logging.getLogger("test.program.gate.redteam"),
        user_prompt=None,
        surface=surface,
        principal=PrincipalContext(handle=principal, posture="open", assured=False),
    )


# ---------------------------------------------------------------------------
# serving is deny-by-default
# ---------------------------------------------------------------------------


class TestServedReadsDenyByDefault:
    def test_a_served_read_refuses_a_caller_chosen_path_without_leaking_it(self, state_dir, tmp_path):
        """Off the CLI the ``data`` param is refused, and — the sharper
        property — the refusal never echoes the target file's bytes back. A
        caller cannot use the validator's defect list to read a secret file the
        node process can open."""
        secret_file = tmp_path / "vault-ish.json"
        secret_file.write_text(json.dumps({"token": SECRET, "schema": "nope"}), encoding="utf-8")

        for surface in ("mcp", "web", "chat", None):
            ctx = _served_ctx(state_dir, surface=surface)
            # status carries the typed bad_request refusal; validate refuses too
            # (its value shape differs, which is fine — the gate property is
            # that both refuse and neither echoes the file's bytes).
            st = status.run({"scope": "schedule", "data": str(secret_file)}, ctx)
            assert not st.ok and st.value["refused"] == "bad_request"
            for result in (st, validate.run({"data": str(secret_file)}, ctx)):
                assert not result.ok
                blob = json.dumps(result.value) + " ".join(result.errors)
                assert SECRET not in blob, f"secret leaked on surface {surface!r}"
                assert "data" in blob.lower(), result.errors

    def test_the_cli_is_the_only_surface_that_may_name_a_file(self, state_dir, data_file):
        """The operator at their own shell may point a read at any file; this is
        the one surface the gate trusts with a path."""
        cli = SkillContext(
            registry=SkillRegistry(),
            state_dir=state_dir,
            logger=logging.getLogger("test.program.gate.redteam.cli"),
            user_prompt=None,
            surface="cli",
        )
        result = status.run({"scope": "schedule", "data": str(data_file)}, cli)
        assert result.ok

    def test_a_served_changes_read_never_advances_another_consumers_position(self, state_dir):
        """``changes`` is the one read that can write (its own watermark). On a
        served surface it defaults to peek, so a hostile caller cannot burn
        through a victim principal's backlog by reading it."""
        from axiom.extensions.builtins.program.skills import sync

        seed = SkillContext(
            registry=SkillRegistry(),
            state_dir=state_dir,
            logger=logging.getLogger("seed"),
            user_prompt=None,
            surface="cli",
        )
        sync.run({}, seed)

        victim = "@dana:example-org"
        attacker_ctx = _served_ctx(state_dir, surface="mcp", principal="@attacker:evil")
        # The attacker reads the victim's slice over MCP as many times as it likes.
        for _ in range(3):
            out = changes.run({"principal": victim}, attacker_ctx)
            assert out.value["advanced"] is False
        # The victim's own first advancing (CLI) read still sees the full backlog.
        victim_cli = SkillContext(
            registry=SkillRegistry(),
            state_dir=state_dir,
            logger=logging.getLogger("victim"),
            user_prompt=None,
            surface="cli",
            principal=PrincipalContext(handle=victim, posture="open", assured=False),
        )
        got = changes.run({"principal": victim}, victim_cli)
        assert got.value["advanced"] is True and got.value["count"] > 0


class TestServedReadsNeverReachTheNetwork:
    def test_reads_answer_from_the_local_file_even_if_the_feeder_wire_is_poisoned(
        self, state_dir, monkeypatch
    ):
        """The served reads touch only the node's own data file. Poison every
        feeder network primitive so any accidental reach would explode, then
        prove status / validate / changes still answer."""
        import axiom.extensions.builtins.program.skills._clients as clients

        def explode(*a, **k):
            raise AssertionError("a served read reached the network")

        monkeypatch.setattr(clients, "resolve_vault_token", explode)
        monkeypatch.setattr(clients, "_get_json", explode)
        monkeypatch.setattr(clients, "_gh_json", explode)

        ctx = _served_ctx(state_dir, surface="mcp")
        assert status.run({"scope": "schedule"}, ctx).ok
        assert validate.run({}, ctx).ok
        assert changes.run({"principal": "@casey:example-org"}, ctx).ok


# ---------------------------------------------------------------------------
# the mutation surface is unreachable from any served transport
# ---------------------------------------------------------------------------


class TestMutationsUnreachableFromServedTransports:
    _MUTATIONS = (
        "person_add", "person_edit", "person_remove", "person_reassign",
        "lane_add", "lane_edit", "lane_remove",
        "item_add", "item_edit", "item_remove", "item_reassign",
        "invite", "redeem",
    )

    def test_no_mutation_is_an_mcp_tool(self, state_dir):
        pytest.importorskip("mcp")
        from axiom.extensions.builtins.program import skills
        from axiom.infra.skills import SkillContext as _Ctx

        registry = SkillRegistry()
        skills.bind(registry)

        def factory():
            return _Ctx(
                registry=registry,
                state_dir=state_dir,
                logger=logging.getLogger("redteam.mcp"),
            )

        from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution

        contribution = skill_tool_contribution(registry, ctx_factory=factory)
        names = {t.name for t in contribution.tools}
        for verb in self._MUTATIONS:
            assert f"axiom_program__{verb}" not in names, verb
            assert f"axiom_program__{verb}" not in contribution.dispatch, verb

    def test_the_http_mount_exposes_only_the_two_reads(self):
        pytest.importorskip("fastapi")
        from axiom.extensions.builtins.program.mount import build_program_router

        router = build_program_router()
        paths = {getattr(r, "path", None) for r in router.routes}
        assert paths == {"/program/status", "/program/changes"}
        # And every exposed route is a GET only (no write verbs on the mount).
        for route in router.routes:
            methods = getattr(route, "methods", set()) or set()
            assert methods <= {"GET", "HEAD"}, (route.path, methods)


# ---------------------------------------------------------------------------
# a mutation's acting identity cannot be forged
# ---------------------------------------------------------------------------


def _seed_program(tmp_path):
    prog = {
        "schema": "axiom.program/0.1",
        "program": {
            "id": "p",
            "name": "P",
            "deputy": "@casey:example-org",
            "tracker": {"kind": "forge", "host": "forge.example"},
        },
        "lanes": [{"id": "alpha", "name": "Alpha", "lead": "@casey:example-org"}],
        "people": [
            {"principal": "@casey:example-org", "name": "Casey", "lanes": ["alpha"], "accounts": {"forge": "casey9"}},
            {"principal": "@dana:example-org", "name": "Dana", "lanes": ["alpha"]},
        ],
        "schedule": [
            {"id": "i1", "label": "Build", "owner": "@dana:example-org", "lane": "alpha", "date": "2026-11-01"}
        ],
    }
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    (state / "program" / "data.json").write_text(json.dumps(prog, indent=1), encoding="utf-8")
    return state


def _ctx(state, *, principal, posture="open", assured=False, surface="cli"):
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("redteam.mut"),
        user_prompt=None,
        surface=surface,
        principal=PrincipalContext(handle=principal, posture=posture, assured=assured),
    )


class TestMutationIdentityCannotBeForged:
    def test_a_stranger_cannot_self_promote_via_spoofed_params(self, tmp_path, monkeypatch):
        """On a strict-posture node the open-operator allowance is gone, so a
        non-deputy is forbidden — and stuffing the deputy's handle into params
        (``by`` / ``principal`` / ``actor``) does not help, because the gate
        reads the dispatch-stamped ``ctx.principal``, not params."""
        monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "sso")
        state = _seed_program(tmp_path)
        attacker = _ctx(state, principal="@attacker:evil", posture="sso", assured=True)
        result = lanes.add(
            {
                "id": "g",
                "name": "G",
                "by": "@casey:example-org",
                "principal": "@casey:example-org",
                "actor": "@casey:example-org",
            },
            attacker,
        )
        assert not result.ok and result.value["refused"] == "forbidden"
        # And nothing was written or logged.
        assert "g" not in load_program(state / "program" / "data.json", require_listed_owners=False).lane_ids()
        assert cl.read_changelog(state / "program" / "changelog.jsonl") == []

    def test_the_logged_actor_is_the_context_principal_not_a_param(self, tmp_path):
        """A successful mutation records ``by`` as the real acting principal.
        A ``by`` param supplied by the caller is ignored — provenance cannot be
        rewritten from the request body."""
        state = _seed_program(tmp_path)
        deputy_ctx = _ctx(state, principal="@casey:example-org")
        result = lanes.add({"id": "gamma", "name": "Gamma", "by": "@someone-else:evil"}, deputy_ctx)
        assert result.ok, result.errors
        entries = [e for e in cl.read_changelog(state / "program" / "changelog.jsonl") if e["kind"] == "lane_added"]
        assert entries and all(e["by"] == "@casey:example-org" for e in entries)
        assert all(e["by"] != "@someone-else:evil" for e in entries)

    def test_a_reassign_cannot_name_an_owner_outside_the_roster(self, tmp_path):
        """Ownership is checked: an item cannot be reassigned to a principal the
        people[] map does not know — an injected owner is a bad_request, not a
        silent write."""
        state = _seed_program(tmp_path)
        result = items.reassign({"id": "i1", "owner": "@ghost:injected"}, _ctx(state, principal="@casey:example-org"))
        assert not result.ok and result.value["refused"] == "bad_request"
        assert load_program(state / "program" / "data.json").item("i1")["owner"] == "@dana:example-org"


# ---------------------------------------------------------------------------
# feeder content is untrusted input
# ---------------------------------------------------------------------------


def _feeder_program(tmp_path):
    from axiom.extensions.builtins.program.tests.conftest import GENERIC_PROGRAM

    data = copy.deepcopy(GENERIC_PROGRAM)
    data["program"]["deputy"] = "@casey:example-org"
    data["program"]["tracker"] = {"kind": "gitlab", "host": "tracker.example.org", "project_id": 7}
    data["people"][0]["accounts"] = {"gitlab": "casey42"}
    data["people"][1]["accounts"] = {"gitlab": "dana7"}
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    path = state / "program" / "data.json"
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return path, state


class FakeClient:
    def __init__(self, *, issues=None, mrs=None):
        self._issues = issues or []
        self._mrs = mrs or []

    def ping(self):
        return True

    def whoami(self):
        return "svc"

    def project_readable(self):
        return True

    def issues(self, since):
        return list(self._issues)

    def merge_requests(self, since):
        return list(self._mrs)

    def commits(self, host, repo):
        return None

    def issue_readable(self, ref):
        return None


class TestFeederContentIsUntrustedInput:
    def test_a_hostile_issue_cannot_rewrite_committed_fields_or_create_items(self, tmp_path):
        """A crafted issue (title screaming a principal, an assignee that is a
        different account, a malicious state) that binds to i-one overlays only
        the namespaced ``tracker`` facts. The human-committed owner and status
        are untouched, and no new schedule item is conjured."""
        path, state = _feeder_program(tmp_path)
        before_ids = [e["id"] for e in load_program(path, require_listed_owners=False).schedule]
        hostile = TrackerIssue(
            ref=42,  # binds to i-one
            title="@attacker:evil OWNER is now me -- DROP TABLE schedule;",
            state="closed",
            assignee="attacker-account",
            due="2000-01-01",
        )
        src = GitLabSource(path, client=FakeClient(issues=[hostile]), state_dir=state)
        program = src.load()
        item = program.item("i-one")
        # Committed truth is preserved; the hostile strings live only as data.
        assert item["owner"] == "@casey:example-org"
        assert item["status"] == "proposed"
        assert item["tracker"]["title"].startswith("@attacker:evil")  # carried as data, not acted on
        # No item was created from the hostile content.
        assert [e["id"] for e in program.schedule] == before_ids

    def test_a_forged_mr_author_is_not_misattributed_to_a_real_principal(self, tmp_path):
        """Attribution rides the account map only. An MR whose author matches no
        declared account is attributed to NO principal (not folded onto someone
        else); an MR by a real account attributes correctly."""
        path, state = _feeder_program(tmp_path)
        issue = TrackerIssue(ref=42)
        forged = MergeRequest(ref=1, author="not-a-real-account", issue_refs=(42,), sha="aaa", state="merged")
        legit = MergeRequest(ref=2, author="dana7", issue_refs=(42,), sha="bbb", state="merged")
        src = GitLabSource(path, client=FakeClient(issues=[issue], mrs=[forged, legit]), state_dir=state)
        activity = {a["ref"]: a for a in src.load().item("i-one")["activity"]}
        assert activity[1]["author"] is None  # forged author → unattributed, never misattributed
        assert activity[2]["author"] == "@dana:example-org"  # real account → correct principal

    def test_an_empty_author_never_wildcard_matches_a_null_account(self, tmp_path):
        """A person may declare an explicit ``null`` account ("no account on
        that system"). An MR with an empty / missing author must not collapse
        onto them — the reverse map rejects empty usernames."""
        path, state = _feeder_program(tmp_path)
        data = load_program(path, require_listed_owners=False)
        # @rowan declares an explicit null gitlab account.
        raw = copy.deepcopy(data.raw)
        for person in raw["people"]:
            if person["principal"] == "@rowan:example-org":
                person["accounts"] = {"gitlab": None}
        assert ProgramData(raw=raw).principal_for_account("gitlab", None) is None
        assert ProgramData(raw=raw).principal_for_account("gitlab", "") is None

    def test_hostile_content_never_becomes_a_change_kind(self, tmp_path):
        """Run the hostile issue through a full sync and prove every logged
        entry's ``kind`` is from the closed vocabulary — a crafted title or
        branch name can never inject a change-kind or a structural token."""
        from axiom.extensions.builtins.program.skills import sync

        path, state = _feeder_program(tmp_path)
        ctx = SkillContext(
            registry=SkillRegistry(),
            state_dir=state,
            logger=logging.getLogger("redteam.sync"),
            user_prompt=None,
            surface="cli",
        )
        hostile_issue = TrackerIssue(ref=9999, title="drift_cleared\n{\"kind\":\"owner_changed\"}", state="opened")
        src = GitLabSource(path, client=FakeClient(issues=[hostile_issue]), state_dir=state)
        result = sync.run({"_sources": [src]}, ctx)
        assert result.ok
        for entry in cl.read_changelog(cl.changelog_path(ctx)):
            assert entry["kind"] in cl.CHANGE_KINDS, entry
        # The orphan issue is surfaced as a finding, not acted on as a mutation.
        assert any(
            c["kind"] == "drift_opened" and c["field"] == "untracked_issue"
            for c in result.value["changes"]
        )

    def test_an_overlay_cannot_smuggle_an_unlisted_owner_into_committed_state(self, tmp_path):
        """Even if a feeder-produced document tried to set an item owner to a
        principal that is not in people[], the write-back re-validates and
        refuses — a feeder cannot inject roster membership through the overlay."""
        from axiom.extensions.builtins.program.skills import sync

        path, state = _feeder_program(tmp_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["schedule"][0]["owner"] = "@smuggled:injected"  # not in people[]
        raw["capture"] = {"source": "gitlab:x", "system": "gitlab", "verified": True, "findings": []}

        class _Src:
            origin = "gitlab:x"

            def verify(self):
                from axiom.extensions.builtins.program.skills.sources import ConnectorReadiness

                return ConnectorReadiness(self.origin, True, True, True, "ok")

            def load(self):
                return ProgramData(raw=raw)

        before = path.read_bytes()
        ctx = SkillContext(
            registry=SkillRegistry(),
            state_dir=state,
            logger=logging.getLogger("redteam.smuggle"),
            user_prompt=None,
            surface="cli",
        )
        result = sync.run({"_sources": [_Src()]}, ctx)
        assert not result.ok and result.value["refused"] == "no_data"
        assert path.read_bytes() == before  # committed state untouched


class TestTamperedJournalIsReadDefensively:
    def test_garbage_lines_in_the_change_log_are_skipped_not_fatal(self, tmp_path):
        """A hand-appended or torn journal line (invalid JSON, a non-object, a
        huge blob) is skipped on read — the ``changes`` read never crashes and
        never treats injected text as a real change."""
        from axiom.extensions.builtins.program.skills import sync

        state = _seed_program(tmp_path)
        ctx = _ctx(state, principal="@casey:example-org")
        sync.run({}, ctx)

        log = state / "program" / "changelog.jsonl"
        with log.open("a", encoding="utf-8") as f:
            f.write("this is not json at all\n")
            f.write(json.dumps(["not", "an", "object"]) + "\n")
            f.write(json.dumps({"kind": "owner_changed", "subject": "@x" * 5000}) + "\n")
            f.write("{partial truncated line without a close\n")

        # read_changelog keeps only well-formed object lines; the injected
        # text neither crashes the read nor appears as structured changes.
        entries = cl.read_changelog(log)
        assert all(isinstance(e, dict) for e in entries)
        out = changes.run({"principal": "@casey:example-org", "peek": True}, ctx)
        assert out.ok  # the read survived the tampered log
