# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The disk-pressure ladder: margin to the kubelet's eviction line, not percent used.

A node went down because a shared volume filled past the kubelet's hard
eviction floor, and the default minimum reclaim kept the pressure on long after
files were freed. Percent-used thresholds cannot see either fact: 96 % of a
3.3 TB volume is 130 GB free, which is plenty for most hosts and two hours from
an eviction on this one. So the ladder measures the margin in bytes to the
line that actually evicts, and it counts the minimum reclaim when pressure
would stay on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.hygiene import disk_ladder as dl

GB = 10**9
GiB = 2**30
TB = 10**12

NOW = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)


def _configz(hard=None, reclaim=None) -> dict:
    cfg: dict = {"kubeletconfig": {"evictionHard": hard or {}}}
    if reclaim is not None:
        cfg["kubeletconfig"]["evictionMinimumReclaim"] = reclaim
    return cfg


# --------------------------------------------------------------------------
# Quantities and the kubelet's own policy
# --------------------------------------------------------------------------


class TestQuantities:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("50Gi", 50 * GiB),
            ("5Gi", 5 * GiB),
            ("500Mi", 500 * 2**20),
            ("1G", GB),
            ("2T", 2 * TB),
            ("1024", 1024),
            ("1.5Gi", int(1.5 * GiB)),
        ],
    )
    def test_absolute_quantities(self, text, expected):
        assert dl.parse_quantity(text) == expected

    def test_a_percentage_is_of_the_volume_it_governs(self):
        assert dl.parse_quantity("10%", total_bytes=3300 * GB) == 330 * GB

    def test_a_percentage_without_a_total_cannot_be_resolved(self):
        assert dl.parse_quantity("10%") is None

    def test_garbage_is_not_a_quantity(self):
        assert dl.parse_quantity("lots") is None
        assert dl.parse_quantity("") is None


class TestEvictionPolicy:
    def test_reads_hard_and_minimum_reclaim_from_configz(self):
        policy = dl.EvictionPolicy.from_configz(
            _configz(
                hard={"nodefs.available": "50Gi", "imagefs.available": "50Gi"},
                reclaim={"nodefs.available": "5Gi", "imagefs.available": "5Gi"},
            )
        )
        assert policy.source == "configz"
        assert policy.floor_bytes(("nodefs", "imagefs"), 3300 * GB) == 50 * GiB
        assert policy.reclaim_bytes(("nodefs", "imagefs"), 3300 * GB) == 5 * GiB

    def test_the_strictest_signal_governs_a_shared_volume(self):
        policy = dl.EvictionPolicy.from_configz(
            _configz(hard={"nodefs.available": "10%", "imagefs.available": "15%"})
        )
        assert policy.floor_bytes(("nodefs", "imagefs"), 1000 * GB) == 150 * GB

    def test_a_volume_no_signal_governs_has_no_floor(self):
        policy = dl.EvictionPolicy.from_configz(_configz(hard={"nodefs.available": "50Gi"}))
        assert policy.floor_bytes((), 1000 * GB) == 0
        assert policy.reclaim_bytes((), 1000 * GB) == 0

    def test_no_kubelet_means_no_floor(self):
        policy = dl.EvictionPolicy.none()
        assert policy.floor_bytes(("nodefs",), 1000 * GB) == 0
        assert policy.source == "none"

    def test_memory_signals_are_ignored(self):
        policy = dl.EvictionPolicy.from_configz(
            _configz(hard={"memory.available": "100Mi", "nodefs.available": "50Gi"})
        )
        assert policy.floor_bytes(("nodefs",), 1000 * GB) == 50 * GiB


# --------------------------------------------------------------------------
# Threshold math
# --------------------------------------------------------------------------


def _assess(free_gb, *, floor="50Gi", reclaim="5Gi", total=3300 * GB, pressure=None, thr=None):
    policy = dl.EvictionPolicy.from_configz(
        _configz(
            hard={"nodefs.available": floor, "imagefs.available": floor},
            reclaim={"nodefs.available": reclaim, "imagefs.available": reclaim},
        )
    )
    reading = dl.VolumeReading(
        name="data-root", path="/srv/data", signals=("nodefs", "imagefs"),
        total_bytes=total, free_bytes=int(free_gb * GB),
    )
    return dl.assess_volume(reading, policy, thr or dl.LadderThresholds(), pressure_on=pressure)


class TestThresholdMath:
    def test_the_margin_is_free_space_above_the_eviction_line(self):
        v = _assess(500)
        floor_gb = 50 * GiB / GB
        assert v["floor_gb"] == pytest.approx(floor_gb, abs=0.01)
        assert v["margin_gb"] == pytest.approx(500 - floor_gb, abs=0.01)
        assert v["rung"] == "ok"

    def test_warn_below_150_gb_of_margin(self):
        floor_gb = 50 * GiB / GB
        assert _assess(floor_gb + 151)["rung"] == "ok"
        assert _assess(floor_gb + 149)["rung"] == "warn"

    def test_page_below_80_gb_of_margin(self):
        floor_gb = 50 * GiB / GB
        assert _assess(floor_gb + 81)["rung"] == "warn"
        assert _assess(floor_gb + 79)["rung"] == "page"

    def test_critical_below_the_floor_plus_20_gb(self):
        floor_gb = 50 * GiB / GB
        assert _assess(floor_gb + 21)["rung"] == "page"
        assert _assess(floor_gb + 19)["rung"] == "critical"

    def test_thresholds_are_configurable_per_site(self):
        thr = dl.LadderThresholds(warn_margin_gb=500, page_margin_gb=300, critical_over_floor_gb=100)
        floor_gb = 50 * GiB / GB
        assert _assess(floor_gb + 400, thr=thr)["rung"] == "warn"
        assert _assess(floor_gb + 200, thr=thr)["rung"] == "page"
        assert _assess(floor_gb + 90, thr=thr)["rung"] == "critical"

    def test_a_volume_without_a_kubelet_floor_measures_from_zero(self):
        policy = dl.EvictionPolicy.none()
        reading = dl.VolumeReading(
            name="bronze", path="/srv/bronze", signals=(), total_bytes=TB, free_bytes=100 * GB
        )
        v = dl.assess_volume(reading, policy, dl.LadderThresholds(), pressure_on=None)
        assert v["floor_gb"] == 0
        assert v["margin_gb"] == pytest.approx(100)
        assert v["rung"] == "warn"

    def test_an_unreadable_volume_is_reported_not_guessed(self):
        reading = dl.VolumeReading(
            name="pv", path="/nope", signals=(), total_bytes=None, free_bytes=None,
            error="No such file or directory",
        )
        v = dl.assess_volume(reading, dl.EvictionPolicy.none(), dl.LadderThresholds(), None)
        assert v["rung"] == "unknown"
        assert "No such file" in v["reasons"][0]


class TestMinimumReclaim:
    """The pitfall from the outage: pressure that will not clear."""

    def test_under_pressure_the_line_is_floor_plus_minimum_reclaim(self):
        # 10 % of 3.3 TB = 330 GB of minimum reclaim: freeing files to 120 GB
        # free does nothing, because pressure clears only at floor + 330 GB.
        v = _assess(120, reclaim="10%", pressure=True)
        floor_gb = 50 * GiB / GB
        assert v["recovery_line_gb"] == pytest.approx(floor_gb + 330, abs=0.01)
        assert v["effective_floor_gb"] == pytest.approx(floor_gb + 330, abs=0.01)
        assert v["margin_gb"] == pytest.approx(120 - floor_gb - 330, abs=0.01)
        assert v["rung"] == "critical"
        assert any("stays on until" in r for r in v["reasons"])

    def test_without_pressure_the_line_is_the_floor(self):
        v = _assess(500, reclaim="10%", pressure=False)
        floor_gb = 50 * GiB / GB
        assert v["effective_floor_gb"] == pytest.approx(floor_gb, abs=0.01)

    def test_below_the_floor_is_pressure_even_when_nobody_said_so(self):
        v = _assess(40, pressure=None)
        assert v["pressure_on"] is True
        assert v["rung"] == "critical"

    def test_a_reclaim_the_volume_cannot_reach_is_at_least_a_warning(self):
        # Free space is below floor + reclaim: if pressure starts it cannot be
        # cleared without freeing more than the volume holds today.
        v = _assess(300, reclaim="10%", pressure=False)
        assert v["rung"] == "warn"
        assert any("minimum reclaim" in r for r in v["reasons"])

    def test_a_small_reclaim_does_not_raise_the_rung(self):
        assert _assess(500, reclaim="5Gi", pressure=False)["rung"] == "ok"


# --------------------------------------------------------------------------
# Images the kubelet cannot pull back
# --------------------------------------------------------------------------


class TestImages:
    def test_a_missing_never_pull_image_pages_with_the_reimport_command(self):
        out = dl.assess_images(
            [dl.ImageCheck(image="db-image:1.0", on_node=False, on_host=True)],
            cluster="dev",
        )
        assert out[0]["rung"] == "page"
        assert "k3d image import db-image:1.0 -c dev" in out[0]["remedy"]

    def test_a_missing_image_not_on_the_host_must_be_rebuilt(self):
        out = dl.assess_images(
            [dl.ImageCheck(image="db-image:1.0", on_node=False, on_host=False)], cluster="dev"
        )
        assert out[0]["rung"] == "page"
        assert "rebuild" in out[0]["remedy"]

    def test_a_present_image_is_ok(self):
        out = dl.assess_images([dl.ImageCheck(image="db-image:1.0", on_node=True, on_host=None)])
        assert out[0]["rung"] == "ok"

    def test_an_image_whose_presence_is_unknown_says_so(self):
        out = dl.assess_images([dl.ImageCheck(image="db-image:1.0", on_node=None, on_host=None)])
        assert out[0]["rung"] == "unknown"

    def test_a_report_that_read_no_volume_is_unknown_not_ok(self):
        unread = dl.assess_volume(
            dl.VolumeReading("pv", "/nope", (), None, None, error="gone"),
            dl.EvictionPolicy.none(), dl.LadderThresholds(), None,
        )
        report = dl.build_report(
            volumes=[unread],
            images=dl.assess_images([dl.ImageCheck("db-image:1.0", on_node=True)]),
            policy=dl.EvictionPolicy.none(), thresholds=dl.LadderThresholds(), now=NOW,
        )
        assert report["rung"] == "unknown"
        assert any("could not read pv" in f for f in report["findings"])

    def test_a_missing_image_raises_the_report_rung(self):
        report = dl.build_report(
            volumes=[_assess(1000)],
            images=dl.assess_images([dl.ImageCheck("db-image:1.0", on_node=False, on_host=True)]),
            policy=dl.EvictionPolicy.none(), thresholds=dl.LadderThresholds(), now=NOW,
        )
        assert report["rung"] == "page"


# --------------------------------------------------------------------------
# Notification decisions
# --------------------------------------------------------------------------


class TestNotificationDecision:
    def _report(self, rung):
        return {"rung": rung, "summary": f"disk {rung}", "checked_at": NOW.isoformat()}

    def test_ok_after_ok_is_silent(self):
        assert not dl.should_notify(self._report("ok"), {"rung": "ok"}, dl.LadderThresholds(), NOW)

    def test_first_run_at_ok_is_silent(self):
        assert not dl.should_notify(self._report("ok"), None, dl.LadderThresholds(), NOW)

    def test_every_change_of_rung_notifies(self):
        thr = dl.LadderThresholds()
        assert dl.should_notify(self._report("warn"), {"rung": "ok"}, thr, NOW)
        assert dl.should_notify(self._report("page"), {"rung": "warn"}, thr, NOW)
        assert dl.should_notify(self._report("ok"), {"rung": "page"}, thr, NOW)

    def test_a_page_repeats_after_the_interval_and_not_before(self):
        thr = dl.LadderThresholds(repeat_minutes=60)
        recent = {"rung": "page", "notified_at": (NOW - timedelta(minutes=30)).isoformat()}
        stale = {"rung": "page", "notified_at": (NOW - timedelta(minutes=61)).isoformat()}
        assert not dl.should_notify(self._report("page"), recent, thr, NOW)
        assert dl.should_notify(self._report("page"), stale, thr, NOW)

    def test_a_warning_repeats_daily_only(self):
        thr = dl.LadderThresholds()
        hours_ago = {"rung": "warn", "notified_at": (NOW - timedelta(hours=5)).isoformat()}
        day_ago = {"rung": "warn", "notified_at": (NOW - timedelta(hours=25)).isoformat()}
        assert not dl.should_notify(self._report("warn"), hours_ago, thr, NOW)
        assert dl.should_notify(self._report("warn"), day_ago, thr, NOW)

    def test_priority_follows_the_rung(self):
        assert dl.PRIORITY["warn"] == "normal"
        assert dl.PRIORITY["page"] == "high"
        assert dl.PRIORITY["critical"] == "urgent"


# --------------------------------------------------------------------------
# The whole watch: probes in, notifications out
# --------------------------------------------------------------------------


class FakeProbes:
    """A test volume that fills on demand, behind a kubelet with a 50Gi floor."""

    def __init__(self, free_gb: float, *, reclaim="5Gi", images=None, pressure=None):
        self.free_gb = free_gb
        self.reclaim = reclaim
        self.images = images or []
        self.pressure = pressure

    def usage(self, path):
        return 3300 * GB, int(self.free_gb * GB)

    def kubelet(self, cfg):
        return dl.KubeletView(
            policy=dl.EvictionPolicy.from_configz(
                _configz(
                    hard={"nodefs.available": "50Gi", "imagefs.available": "50Gi"},
                    reclaim={"nodefs.available": self.reclaim, "imagefs.available": self.reclaim},
                )
            ),
            pressure_on=self.pressure,
            images=list(self.images),
            node="node-0",
        )


def _cfg(**over) -> dl.LadderConfig:
    base = dict(
        volumes=[dl.VolumeSpec(name="data-root", path="/srv/data", signals=("nodefs", "imagefs"))],
        thresholds=dl.LadderThresholds(),
        recipient="@operator:local",
        cluster="dev",
    )
    base.update(over)
    return dl.LadderConfig(**base)


@pytest.mark.acceptance("MVP-19-A1")
def test_mvp_19_a1_filling_a_volume_raises_warn_page_critical_in_order_each_notified(tmp_path):
    """MVP-19-A1: a test volume filling past each rung raises it, in order, and
    each rung reaches a person through the notifications inbox."""
    sent: list[dict] = []
    state = tmp_path / "disk-ladder.json"
    floor_gb = 50 * GiB / GB
    probes = FakeProbes(free_gb=floor_gb + 400)
    seen: list[str] = []
    for i, free in enumerate([400, 140, 70, 15]):
        probes.free_gb = floor_gb + free
        result = dl.watch(
            _cfg(), probes=probes, notify=sent.append, state_path=state,
            now=NOW + timedelta(minutes=5 * i),
        )
        seen.append(result["rung"])
    assert seen == ["ok", "warn", "page", "critical"]
    assert [n["rung"] for n in sent] == ["warn", "page", "critical"]
    assert [n["priority"] for n in sent] == ["normal", "high", "urgent"]
    assert all(n["recipient"] == "@operator:local" for n in sent)
    assert all(n["dedup_key"] for n in sent)
    assert len({n["dedup_key"] for n in sent}) == 3


def test_a_repeat_run_at_the_same_rung_does_not_renotify(tmp_path):
    sent: list[dict] = []
    state = tmp_path / "disk-ladder.json"
    probes = FakeProbes(free_gb=50 * GiB / GB + 70)
    dl.watch(_cfg(), probes=probes, notify=sent.append, state_path=state, now=NOW)
    dl.watch(_cfg(), probes=probes, notify=sent.append, state_path=state,
             now=NOW + timedelta(minutes=5))
    assert len(sent) == 1


def test_recovery_is_notified_once(tmp_path):
    sent: list[dict] = []
    state = tmp_path / "disk-ladder.json"
    probes = FakeProbes(free_gb=50 * GiB / GB + 70)
    dl.watch(_cfg(), probes=probes, notify=sent.append, state_path=state, now=NOW)
    probes.free_gb = 1000
    dl.watch(_cfg(), probes=probes, notify=sent.append, state_path=state,
             now=NOW + timedelta(minutes=5))
    dl.watch(_cfg(), probes=probes, notify=sent.append, state_path=state,
             now=NOW + timedelta(minutes=10))
    assert [n["rung"] for n in sent] == ["page", "ok"]
    assert "recovered" in sent[1]["summary"]


def test_a_failed_notification_is_retried_next_run(tmp_path):
    state = tmp_path / "disk-ladder.json"
    probes = FakeProbes(free_gb=50 * GiB / GB + 70)

    def broken(_):
        raise RuntimeError("inbox down")

    first = dl.watch(_cfg(), probes=probes, notify=broken, state_path=state, now=NOW)
    assert first["notified"] is False
    assert any("inbox down" in e for e in first["notify_errors"])
    sent: list[dict] = []
    dl.watch(_cfg(), probes=probes, notify=sent.append, state_path=state,
             now=NOW + timedelta(minutes=5))
    assert len(sent) == 1


def test_the_minimum_reclaim_trap_is_named_in_the_alert(tmp_path):
    sent: list[dict] = []
    probes = FakeProbes(free_gb=120, reclaim="10%", pressure=True)
    dl.watch(_cfg(), probes=probes, notify=sent.append, state_path=tmp_path / "s.json", now=NOW)
    assert sent[0]["rung"] == "critical"
    assert "minimum reclaim" in sent[0]["body"]


def test_a_missing_image_is_in_the_alert_with_its_reimport(tmp_path):
    sent: list[dict] = []
    probes = FakeProbes(
        free_gb=2000, images=[dl.ImageCheck("db-image:1.0", on_node=False, on_host=True)]
    )
    out = dl.watch(_cfg(), probes=probes, notify=sent.append, state_path=tmp_path / "s.json",
                   now=NOW)
    assert out["rung"] == "page"
    assert "k3d image import db-image:1.0 -c dev" in sent[0]["body"]


def test_the_status_line_is_one_line_and_names_the_tightest_volume(tmp_path):
    probes = FakeProbes(free_gb=50 * GiB / GB + 70)
    out = dl.watch(_cfg(), probes=probes, notify=lambda _: None,
                   state_path=tmp_path / "s.json", now=NOW)
    line = dl.status_line(out)
    assert "\n" not in line
    assert line.startswith("page")
    assert "data-root" in line
    assert "70 GB" in line


def test_the_status_line_reads_the_saved_state_and_says_when_it_is_stale(tmp_path):
    state = tmp_path / "s.json"
    dl.watch(_cfg(), probes=FakeProbes(free_gb=2000), notify=lambda _: None,
             state_path=state, now=NOW)
    fresh = dl.last_status(state, now=NOW + timedelta(minutes=4))
    stale = dl.last_status(state, now=NOW + timedelta(hours=2))
    assert fresh["rung"] == "ok" and not fresh["stale"]
    assert stale["stale"] is True
    assert dl.last_status(tmp_path / "absent.json", now=NOW) is None


# --------------------------------------------------------------------------
# Site configuration
# --------------------------------------------------------------------------


def test_the_site_config_sets_volumes_thresholds_and_recipient(tmp_path):
    path = tmp_path / "disk-ladder.toml"
    path.write_text(
        """
[ladder]
warn_margin_gb = 200
page_margin_gb = 100
critical_over_floor_gb = 30
repeat_minutes = 30
recipient = "@ops:site"

[kubelet]
cluster = "edge"
kubectl_context = "k3d-edge"

[[volume]]
name = "data-root"
path = "/srv/docker"
signals = ["nodefs", "imagefs"]

[[volume]]
name = "bronze"
path = "/srv/bronze"
""",
        encoding="utf-8",
    )
    cfg = dl.load_config(path)
    assert cfg.thresholds == dl.LadderThresholds(
        warn_margin_gb=200, page_margin_gb=100, critical_over_floor_gb=30, repeat_minutes=30
    )
    assert cfg.recipient == "@ops:site"
    assert cfg.cluster == "edge"
    assert cfg.kubectl_context == "k3d-edge"
    assert [v.name for v in cfg.volumes] == ["data-root", "bronze"]
    assert cfg.volumes[0].signals == ("nodefs", "imagefs")
    assert cfg.volumes[1].signals == ()


def test_a_page_threshold_above_the_warn_threshold_is_refused(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[ladder]\nwarn_margin_gb = 50\npage_margin_gb = 80\n", encoding="utf-8")
    with pytest.raises(ValueError, match="page_margin_gb"):
        dl.load_config(path)
