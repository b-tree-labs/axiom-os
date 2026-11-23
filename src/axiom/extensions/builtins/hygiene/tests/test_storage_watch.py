# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Storage cap classification, default agency, and the artifacts skill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from axiom.extensions.builtins.hygiene.artifact_cleanup import Artifact, CacheRecord, StoragePolicy
from axiom.extensions.builtins.hygiene.agency import BlastCaps, storage_enabled, storage_watch_only
from axiom.extensions.builtins.hygiene.skills.artifacts import run_storage, storage_digest, storage_tick
from axiom.extensions.builtins.hygiene.storage_audit import audit_path
from axiom.extensions.builtins.hygiene.storage_watch import (
    GIB,
    StorageReading,
    accrued_gb_hours_from_usage_summary,
    classify_thresholds,
)
from axiom.infra.skills import SkillContext, SkillRegistry


NOW = datetime(2026, 11, 21, 14, 40, tzinfo=timezone.utc)


def _ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=None, user_prompt=None)


def _art(id: str, **kwargs) -> Artifact:
    branch = kwargs.pop("branch", "main")
    name = kwargs.pop("name", "dist")
    days = kwargs.pop("days_ago", 0)
    size = kwargs.pop("size", 36 * 1024 * 1024)
    repo = kwargs.pop("repo", "acme/repo")
    return Artifact(
        id=id,
        name=name,
        size_bytes=size,
        created_at=NOW - timedelta(days=days),
        workflow_run_id=f"run-{id}",
        provider="github",
        repo=repo,
        head_branch=branch,
    )


def _cache(id: str, ref: str, size: int = 100) -> CacheRecord:
    return CacheRecord(
        id=id, key=id, ref=ref, size_bytes=size, created_at=NOW,
        provider="github", repo="acme/repo",
    )


class _StubProvider:
    name = "github"

    def __init__(self, artifacts, caches=None):
        self._artifacts = artifacts
        self._caches = caches or []
        self.deleted_arts: list[str] = []
        self.deleted_caches: list[str] = []

    def list_artifacts(self, repo: str):
        return list(self._artifacts)

    def list_caches(self, repo: str):
        return list(self._caches)

    def delete_artifact(self, a: Artifact) -> bool:
        self.deleted_arts.append(a.id)
        return True

    def delete_cache(self, c: CacheRecord) -> bool:
        self.deleted_caches.append(c.id)
        return True


def _ok_gh(*args, **kwargs):
    return SimpleNamespace(returncode=0, stdout="[]", stderr="")


class TestClassifyThresholds:
    def test_under_warn_is_silent(self):
        reading = StorageReading(artifact_bytes=int(1.0 * GIB))  # 50% of 2 GB
        assert classify_thresholds(reading) == []

    def test_70_percent_warns(self):
        reading = StorageReading(artifact_bytes=int(1.5 * GIB))  # 75%
        crossings = classify_thresholds(reading)
        assert any(c.kind == "shared_current" and c.severity == "warn" for c in crossings)

    def test_85_percent_escalates(self):
        reading = StorageReading(artifact_bytes=int(1.8 * GIB))  # 90%
        crossings = classify_thresholds(reading)
        assert any(c.kind == "shared_current" and c.severity == "escalate" for c in crossings)

    def test_accrued_and_cache_caps(self):
        reading = StorageReading(
            artifact_bytes=0,
            accrued_gb_hours=1300,  # 90% of 1440
            cache_bytes_by_repo={"acme/repo": int(9.0 * GIB)},
        )
        kinds = {(c.kind, c.severity) for c in classify_thresholds(reading)}
        assert ("shared_accrued", "escalate") in kinds
        assert ("cache", "escalate") in kinds

    def test_usage_summary_parser(self):
        payload = {
            "usageItems": [
                {"product": "Actions", "sku": "actions_storage", "grossQuantity": 1367.2},
                {"product": "Actions", "sku": "actions_linux", "grossQuantity": 100},
            ]
        }
        assert accrued_gb_hours_from_usage_summary(payload) == 1367.2


def _base_params(tmp_path, provider, **extra):
    params = {
        "repo": "acme/repo",
        "state_dir": tmp_path,
        "now": NOW,
        "_provider": provider,
        "_runner": _ok_gh,
        "_open_prs": {"acme/repo": set()},
        "_closed_or_merged": {("acme/repo", 7): True},
        "policy": StoragePolicy(),
        "_skip_rate_limit": True,
    }
    params.update(extra)
    return params


class TestWatchOnlySkill:
    def test_setting_defaults_false(self, monkeypatch):
        monkeypatch.delenv("AXIOM_HYGIENE_STORAGE_WATCH_ONLY", raising=False)
        monkeypatch.delenv("AXIOM_HYGIENE_STORAGE_ENABLED", raising=False)
        assert storage_watch_only(lambda key, default=None: default) is False
        assert storage_enabled(lambda key, default=None: default) is True

    def test_dry_run_does_not_delete_and_writes_audit(self, tmp_path):
        provider = _StubProvider(
            [_art("keep", days_ago=0), _art("old", days_ago=0.2), _art("older", days_ago=4)],
            [_cache("merged", "refs/pull/7/merge", size=50)],
        )
        # three main dists today — policy keeps 2 newest, deletes "older"
        # created_at: keep is NOW, old is 0.2d, older is 1d
        result = run_storage(
            {
                "repo": "acme/repo",
                "watch_only": True,
                "state_dir": tmp_path,
                "now": NOW,
                "_provider": provider,
                "_runner": _ok_gh,
                "_open_prs": {"acme/repo": set()},
                "_closed_or_merged": {("acme/repo", 7): True},
                "_reading": StorageReading(artifact_bytes=int(1.6 * GIB)),
                "recipients": ("@operator",),
                "_sender": lambda recipient, alert: "receipt-1",
                "policy": StoragePolicy(),
            },
            _ctx(tmp_path),
        )
        assert result.ok
        assert result.value["watch_only"] is True
        assert provider.deleted_arts == []
        assert provider.deleted_caches == []
        assert result.value["candidates"]["artifacts"] == 1
        assert result.value["candidates"]["caches"] == 1
        assert any(c["severity"] == "warn" for c in result.value["crossings"])
        assert audit_path(tmp_path).is_file()
        log = audit_path(tmp_path).read_text()
        assert "watch_only" in log
        assert "TOKEN" not in log and "SECRET" not in log

    def test_enforce_deletes_inside_policy(self, tmp_path):
        provider = _StubProvider(
            [_art("keep", days_ago=0), _art("old", days_ago=0.2), _art("older", days_ago=4)],
            [_cache("merged", "refs/pull/7/merge"), _cache("main", "refs/heads/main")],
        )
        result = run_storage(
            {
                "repo": "acme/repo",
                "enforce": True,
                "state_dir": tmp_path,
                "now": NOW,
                "_provider": provider,
                "_runner": _ok_gh,
                "_open_prs": {"acme/repo": set()},
                "_closed_or_merged": {("acme/repo", 7): True},
                "policy": StoragePolicy(),
            },
            _ctx(tmp_path),
        )
        assert result.ok
        assert result.value["watch_only"] is False
        assert set(provider.deleted_arts) == {"older"}
        assert provider.deleted_caches == ["merged"]
        assert "main" not in provider.deleted_caches

    def test_default_agency_deletes_inside_policy(self, tmp_path):
        provider = _StubProvider(
            [_art("keep", days_ago=0), _art("old", days_ago=0.2), _art("older", days_ago=4)],
            [_cache("merged", "refs/pull/7/merge"), _cache("main", "refs/heads/main")],
        )
        result = run_storage(_base_params(tmp_path, provider), _ctx(tmp_path))
        assert result.ok
        assert result.value["watch_only"] is False
        assert result.value["act_reason"] == "default agency"
        assert set(provider.deleted_arts) == {"older"}
        assert provider.deleted_caches == ["merged"]
        assert "main" not in provider.deleted_caches
        assert "Recovery" in "\n".join(result.actions_taken)
        log = audit_path(tmp_path).read_text()
        assert "recovery" in log
        assert "TOKEN" not in log and "SECRET" not in log

    def test_kill_switch_blocks_even_enforce(self, tmp_path):
        provider = _StubProvider(
            [_art("keep"), _art("old", days_ago=0.2), _art("older", days_ago=4)],
        )
        result = run_storage(
            _base_params(
                tmp_path,
                provider,
                enforce=True,
                _settings_get=lambda key, default=None: False if key == "hygiene.storage_enabled" else default,
            ),
            _ctx(tmp_path),
        )
        assert result.ok
        assert result.value["watch_only"] is True
        assert "kill switch" in result.value["act_reason"]
        assert provider.deleted_arts == []

    def test_blast_caps_defer_overflow(self, tmp_path):
        provider = _StubProvider(
            [
                _art("keep"),
                _art("old", days_ago=0.2),
                _art("older", days_ago=4, size=200),
                _art("oldest", days_ago=5, size=50),
            ],
        )
        result = run_storage(
            _base_params(tmp_path, provider, caps=BlastCaps(max_items=1, max_gib=10)),
            _ctx(tmp_path),
        )
        assert result.ok
        assert result.value["deleted"]["artifacts"] == 1
        assert result.value["deferred"]["artifacts"] == 1
        assert provider.deleted_arts == ["older"]  # larger first

    def test_mcp_without_enforce_stays_dry_run(self, tmp_path):
        provider = _StubProvider(
            [_art("keep"), _art("old", days_ago=0.2), _art("older", days_ago=4)],
        )
        result = run_storage(
            _base_params(tmp_path, provider, surface="mcp"),
            _ctx(tmp_path),
        )
        assert result.ok
        assert result.value["watch_only"] is True
        assert provider.deleted_arts == []

    def test_tick_acts_and_85_percent_files_needs_you(self, tmp_path):
        # One huge kept dist (never-touch: newest of two) so reclaim cannot drop under 85%.
        kept = _art("keep", size=int(1.9 * (1024**3)))
        older = _art("older", days_ago=4, size=int(0.05 * (1024**3)))
        mid = _art("old", days_ago=0.2, size=100)
        provider = _StubProvider([kept, mid, older])
        sent = []

        def _sender(recipient, alert):
            sent.append(alert.kind)
            return "r"

        result = storage_tick(
            _base_params(
                tmp_path,
                provider,
                recipients=("@operator",),
                _sender=_sender,
                _reading=__import__(
                    "axiom.extensions.builtins.hygiene.storage_watch", fromlist=["StorageReading"]
                ).StorageReading(artifact_bytes=int(1.95 * (1024**3))),
            ),
            _ctx(tmp_path),
        )
        assert result.ok
        assert result.value["watch_only"] is False
        assert result.value["aggressive"] is True
        assert result.value["needs_you"] is not None
        assert result.value["remaining_pct"] >= 85
        assert any("NEEDS YOU" in line for line in result.actions_taken)
        assert "storage_action" in sent or "shared_current" in sent

    def test_digest_never_deletes(self, tmp_path):
        provider = _StubProvider([_art("keep"), _art("old", days_ago=4), _art("older", days_ago=5)])
        sent = []

        def _sender(recipient, alert):
            sent.append(alert.kind)
            return "r"

        result = storage_digest(
            {
                "repo": "acme/repo",
                "enforce": True,  # must still be ignored
                "state_dir": tmp_path,
                "now": NOW,
                "_provider": provider,
                "_runner": _ok_gh,
                "recipients": ("@operator",),
                "_sender": _sender,
            },
            _ctx(tmp_path),
        )
        assert result.ok
        assert result.value["watch_only"] is True
        assert provider.deleted_arts == []
        assert "weekly_digest" in sent


class TestTickSurfaces:
    def test_tick_skills_are_declared_cli_writes(self):
        from axiom.extensions.builtins.hygiene import skills
        from axiom.infra.skills import SkillRegistry

        reg = SkillRegistry()
        skills.bind(reg)
        for name in ("hygiene.storage_tick", "hygiene.storage_digest", "hygiene.github_tick"):
            spec = reg.spec(name)
            assert spec is not None, name
            assert spec.surfaces == ("cli",), name
            assert spec.side_effects is True
            assert "mcp" not in (spec.surfaces or ())
