# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shadow risk-tier review (ADR-045). The tier is computed and logged.

What Rivet merges or holds is a separate, unchanged decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.release.ci_flow import independent_reviewers
from axiom.extensions.builtins.release.lander.actor import landing_block
from axiom.extensions.builtins.release.lander.config import LanderConfig
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.protect import landing_tier, protected_categories
from axiom.infra.raci import matrix_tier, operating_trust
from axiom.infra.raci_review import (
    DEFAULT_APPROVERS,
    BlastFloor,
    FileChange,
    ReviewRequest,
    deliver,
    evaluate,
    load_review_config,
    observe,
    present_preset,
    read_shadow_log,
    refit_thresholds,
)
from axiom.policy.action_ledger import ActionLedger

ROOT = Path(__file__).resolve().parents[2]
_SEEN = ("seen",)
_SECRET = "AKIA" + "A" * 16


def _req(**kwargs) -> ReviewRequest:
    kwargs.setdefault("seen_scopes", _SEEN)
    kwargs.setdefault("scope", "seen")
    kwargs.setdefault("preset", "autonomous")
    return ReviewRequest(**kwargs)


def test_landing_actions_follow_the_preset_bands():
    for action in (
        "merge",
        "repair",
        "close",
        "rerun",
        "update_branch",
        "update-branch",
        "batch",
        "enqueue",
    ):
        assert matrix_tier(action, 1) == "A"
        assert matrix_tier(action, 2) == "A"
    assert matrix_tier("pr.merge", 3) == "C"
    assert matrix_tier("pr.merge", 4) == "N"
    assert matrix_tier("pr.batch", 4) == "C"
    assert matrix_tier("pr.repair", 5) == "I"
    assert matrix_tier("code.commit", 5) == "A"
    assert operating_trust("autonomous", 2) == 4
    assert operating_trust("autonomous", 5) == 5
    assert operating_trust("strict", None) == 1
    assert operating_trust("balanced", 2) == 3


def test_a_routine_change_is_act_then_notify_and_does_not_land():
    decision = evaluate(_req(action="merge", paths=("src/author.py",), additions=12, deletions=3))
    assert decision.tier == "N"
    assert decision.event == "rivet.land.n_digest"
    assert decision.priority == "low"
    assert decision.live_effect == "unchanged"
    assert decision.shadow is True
    assert landing_tier(("src/author.py",), additions=12, deletions=3) == "N"
    assert protected_categories(("src/author.py",)) == ()


def test_sensitive_path_and_content_signals_move_toward_approval():
    sensitive = evaluate(_req(paths=("src/auth/session.py",)))
    assert sensitive.tier == "C"
    assert sensitive.sensitive_paths == ("src/auth/session.py",)

    sql = evaluate(_req(diff="+DROP TABLE sessions;\n", paths=("migrations/001.sql",)))
    assert sql.tier == "C"
    assert "destructive_sql" in sql.content_signals
    assert sql.floor == ()

    price = evaluate(_req(
        diff="+PRICE = 'price_1N1AbCdEfGhIjKlMnOp'\n",
        paths=("src/billing/prices.py",),
    ))
    assert "live_stripe_price_id" in price.content_signals
    assert price.tier == "C"
    assert price.priority == "urgent"

    fixture = evaluate(_req(
        diff="+PRICE = 'price_1N1AbCdEfGhIjKlMnOp'\n",
        paths=("tests/test_prices.py",),
    ))
    assert "live_stripe_price_id" not in fixture.content_signals


def test_the_always_a_floor_cannot_be_loosened():
    opinions = (None, "low", "high")
    presets = ("strict", "balanced", "autonomous")
    for preset in presets:
        for trust in range(1, 6):
            for opinion in opinions:
                forced = evaluate(ReviewRequest(
                    action="merge",
                    paths=("docs/readme.md",),
                    preset=preset,
                    explicit_trust=trust,
                    model_risk=opinion,
                    force_push_default=True,
                    seen_scopes=_SEEN,
                    scope="seen",
                    proposed_thresholds={"lines_high": 1},
                    refit_approved=True,
                ))
                assert forced.tier == "A", (preset, trust, opinion)
                assert forced.live_effect == "unchanged"
                assert forced.model_advisory == opinion
                assert any("force-push" in item for item in forced.floor)

    secret = evaluate(_req(diff=f"+token = '{_SECRET}'\n", paths=("src/app.py",), model_risk="low"))
    assert secret.tier == "A"
    assert secret.priority == "urgent"
    assert "secrets in diff" in secret.floor

    prod = evaluate(_req(
        diff="+DROP TABLE production.accounts;\n",
        paths=("migrations/prod/010.sql",),
        model_risk="low",
    ))
    assert prod.tier == "A"
    assert any("production data" in item for item in prod.floor)

    rewrite = evaluate(_req(diff="+git push --force origin main\n"))
    assert rewrite.tier == "A"
    assert any("force-push" in item for item in rewrite.floor)


def test_a_model_opinion_does_not_move_the_tier():
    plain = evaluate(_req(paths=("src/author.py",)))
    advised = evaluate(_req(paths=("src/author.py",), model_risk="high"))
    assert plain.tier == advised.tier == "N"
    assert "advisory" in " ".join(advised.reasons)


def test_blast_radius_excludes_noise_and_asks_for_two_reviewers():
    noisy = evaluate(_req(files=(
        FileChange("vendor/lib.js", additions=3000),
        FileChange("src/generated/api.pb.go", additions=9000),
        FileChange("src/__snapshots__/row.snap", additions=2000),
        FileChange("src/old.py", rename=True),
        FileChange("src/author.py", additions=10, deletions=2),
    )))
    assert noisy.blast_band == "low"
    assert noisy.counted_files == 1
    assert noisy.counted_lines == 12
    assert noisy.tier == "N"
    lock = evaluate(_req(files=(
        FileChange("uv.lock", additions=5000, deletions=4000),
        FileChange("src/author.py", additions=10, deletions=2),
    )))
    assert lock.counted_lines == 12
    assert lock.counted_files == 1
    assert "uv.lock" in lock.sensitive_paths
    assert lock.tier == "C"

    medium = evaluate(_req(
        paths=("src/author.py",),
        additions=900,
        deletions=0,
        author_model="writer",
        review_models=("writer", "review-a", "review-b"),
        separate_reviewer=True,
    ))
    assert medium.blast_band == "medium"
    assert medium.dual_review_ok is True
    assert medium.reviewers == ("review-a", "review-b")
    assert medium.tier == "N"

    short = evaluate(_req(
        paths=("src/author.py",),
        additions=900,
        author_model="writer",
        review_models=("writer",),
        separate_reviewer=True,
    ))
    assert short.tier == "A"
    assert short.dual_review_ok is False

    high = evaluate(_req(paths=("src/author.py",), additions=2001))
    assert high.blast_band == "high"
    assert high.tier == "C"
    assert evaluate(_req(paths=tuple(f"src/f{i}.py" for i in range(51)), additions=10)).blast_band == "high"
    assert evaluate(_req(paths=tuple(f"src/f{i}.py" for i in range(50)), additions=10)).blast_band == "medium"


def test_independent_reviewers_reuse_choose_reviewer():
    picked = independent_reviewers("writer", ["writer", "review-a", "review-b"])
    assert picked["ok"] is True
    assert picked["reviewers"] == ["review-a", "review-b"]
    refused = independent_reviewers("writer", ["writer", "review-a"], count=2)
    assert refused["ok"] is False
    assert refused["reviewers"] == ["review-a"]


def test_learn_refits_above_the_floor_and_holds_the_floor_without_approval():
    raised, notes = refit_thresholds(BlastFloor(), {"lines_high": 2500})
    assert raised.lines_high == 2500
    assert any("2500" in note for note in notes)

    held, notes = refit_thresholds(BlastFloor(), {"lines_high": 1000})
    assert held.lines_high == 2000
    assert any("held at floor" in note for note in notes)

    lowered, notes = refit_thresholds(BlastFloor(), {"lines_high": 1000}, approved=True)
    assert lowered.lines_high == 1000
    assert any("with approval" in note for note in notes)

    frozen, notes = refit_thresholds(BlastFloor(learn=False), {"lines_high": 4000})
    assert frozen.lines_high == 2000
    assert notes == ("learn is off; thresholds stay at the configured floor",)

    decision = evaluate(_req(
        paths=("src/author.py",),
        additions=2200,
        proposed_thresholds={"lines_high": 3000, "files_high": 80},
    ))
    assert decision.blast_band == "medium"


def test_volume_brake_and_first_scope_demote_notify_to_consulted():
    brake = evaluate(_req(
        paths=("src/author.py",),
        recent_batch_sizes=(2, 2, 2, 2),
        batch_size=20,
    ))
    assert brake.tier == "C"
    assert brake.volume_brake is True
    assert brake.priority == "normal"
    assert brake.event == "rivet.land.approval_needed"

    rate = evaluate(_req(paths=("src/author.py",), invocations_in_window=11))
    assert rate.volume_brake is True
    assert rate.tier == "C"

    first = evaluate(ReviewRequest(
        action="merge",
        paths=("src/author.py",),
        preset="autonomous",
        repo="acme/app",
        branch="feat/tidy",
    ))
    assert first.first_scope is True
    assert first.tier == "C"
    assert first.priority == "normal"

    again = evaluate(ReviewRequest(
        action="merge",
        paths=("src/author.py",),
        preset="autonomous",
        repo="acme/app",
        branch="feat/tidy",
        seen_scopes=("acme/app|feat|src",),
        scope="acme/app|feat|src",
    ))
    assert again.first_scope is False
    assert again.tier == "N"


def test_gate_weakening_is_approval():
    checks = evaluate(_req(diff=(
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@\n"
        "-    required_status_checks:\n"
        "-      - ci / test\n"
        "+    required_status_checks:\n"
        "+      - ci / lint\n"
    )))
    assert checks.tier == "A"
    assert any("ci / test" in item for item in checks.gate)
    assert checks.priority == "high"

    deploy = evaluate(_req(diff=(
        "-    required_approving_review_count: 2\n"
        "+    required_approving_review_count: 0\n"
    )))
    assert deploy.tier == "A"
    assert deploy.priority == "urgent"
    assert any("required_approving_review_count" in item for item in deploy.gate)

    named = evaluate(_req(removed_required_checks=("ci / test",)))
    assert named.tier == "A"


def test_default_roster_leaves_the_unconfirmed_identity_out():
    assert DEFAULT_APPROVERS == ("@benjaminbooth", "@luker-robinson2")
    assert all("redrossa" not in name for name in DEFAULT_APPROVERS)
    loaded = load_review_config(ROOT / ".axiom" / "raci.toml")
    assert loaded.approvers == DEFAULT_APPROVERS
    assert loaded.approval_count == 1
    assert loaded.blast.lines_high == 2000
    assert loaded.blast.learn is True
    names = [name for _category, roster in loaded.category_approvers for name in roster.roster]
    assert all("redrossa" not in name for name in names)
    security = dict(loaded.category_approvers)["security"]
    assert security.count == 1

    routed = evaluate(_req(
        paths=("src/auth/session.py",),
        diff=f"+token = '{_SECRET}'\n",
    ))
    assert routed.tier == "A"
    assert routed.approvers == DEFAULT_APPROVERS
    assert routed.approvals_required == 1


def test_shadow_log_and_ledger_record_the_decision_without_a_landing_change(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "axiom.infra.raci_review._emit",
        lambda decision: None,
    )
    record = observe(
        _req(paths=("src/author.py",), candidate="pr:17"),
        state_dir=tmp_path,
        live_block="",
        kind="hold",
    )
    assert record["tier"] == "N"
    assert record["live_effect"] == "unchanged"
    assert record["would_change_landing"] is False
    assert record["live_block"] == ""
    rows = read_shadow_log(tmp_path)
    assert rows[0]["event"] == "rivet.land.n_digest"
    assert rows[0]["candidate"] == "pr:17"

    ledger = ActionLedger(state_dir=tmp_path)
    found = ledger.query(agent="rivet", op_class="raci.risk_tier")
    assert found
    assert found[0]["outcome"] == "shadow"
    assert found[0]["dry_run"] is True
    assert found[0]["metadata"]["tier"] == "N"


def test_deliver_reports_through_herald_send(monkeypatch):
    captured: dict = {}

    def fake_send(ctx, **kwargs):
        captured["ctx"] = ctx
        captured.update(kwargs)
        return "sent"

    import importlib
    import sys

    importlib.import_module("axiom.extensions.builtins.notifications.send")
    send_mod = sys.modules["axiom.extensions.builtins.notifications.send"]
    monkeypatch.setattr(send_mod, "send", fake_send)
    decision = evaluate(_req(paths=("src/author.py",)))
    assert deliver(decision, object()) == "sent"
    assert captured["recipient"] == "@operator"
    assert captured["priority"].value == "low"
    assert captured["payload"].metadata["shadow"] is True


def test_noninteractive_install_explains_the_preset_and_prints_the_opt_out(capsys):
    assert present_preset(interactive=False, cli="axi") == "autonomous"
    text = capsys.readouterr().out
    assert "autonomous" in text
    assert "shadow" in text.lower()
    assert "autonomy.enabled" in text
    assert "Opt out: axi settings set raci.preset strict" in text


def test_interactive_install_records_an_opt_out():
    saved: dict = {}
    choice = present_preset(
        interactive=True,
        cli="axi",
        read=lambda: "strict\n",
        write=lambda *_args, **_kwargs: None,
        save=lambda preset: saved.setdefault("preset", preset),
    )
    assert choice == "strict"
    assert saved["preset"] == "strict"


def test_live_landing_block_is_unchanged_while_the_shadow_log_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("axiom.infra.raci_review._emit", lambda decision: None)
    pull = OpenPull(
        number=17,
        title="docs",
        head_sha="abc",
        head_ref="feat/docs",
        files=("src/author.py",),
        checks="passing",
        mergeable=True,
        additions=4,
        deletions=1,
    )
    assert landing_block(pull, LanderConfig()) == ""
    held = OpenPull(
        number=18,
        title="auth",
        head_sha="def",
        head_ref="feat/auth",
        files=("src/auth/session.py",),
        checks="passing",
        mergeable=True,
    )
    assert landing_block(held, LanderConfig()).startswith("protected:")
    rows = read_shadow_log(tmp_path)
    assert rows
    assert all(row["live_effect"] == "unchanged" for row in rows)
    assert all(row["would_change_landing"] is False for row in rows)


@pytest.mark.parametrize(
    "diff",
    [
        "+git rebase origin/main\n",
        "+git filter-repo --path secrets\n",
    ],
)
def test_history_rewrite_is_a_floor(diff):
    decision = evaluate(_req(diff=diff, paths=("scripts/tidy.sh",), model_risk="low"))
    assert decision.tier == "A"
