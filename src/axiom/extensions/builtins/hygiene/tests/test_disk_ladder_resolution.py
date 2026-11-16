# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where the ladder looks: the runtime's data root, the site config, the saved reading.

The 2026-10-31 deploy measured the wrong volume. ``docker info`` needs the
docker socket, which the node's own user cannot open, so
``auto:docker-data-root`` did not resolve; and ``axi hygiene stat disk`` run by
hand did not find the site config (the timer names it in its environment), so
it fell back to ``/var/lib/rancher/k3s`` on ``/`` and called the node critical
while the real data root, ``/home/docker``, had 84 GB free. Separately, the
timer saved its reading under ``~/.axi`` and ``neut status`` looked under
``~/.neut``, so the status line said "not measured yet" with the timer armed.

Each resolution path is tested on its own, and the last resort is to say
"unresolved" and alert, never to measure something else.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.extensions.builtins.hygiene import disk_ladder as dl
from axiom.extensions.builtins.hygiene import disk_probes as dp

GiB = 2**30

PROC_MOUNTS = """\
/dev/mapper/vg-root / ext4 rw,relatime 0 0
/dev/mapper/vg-home /home ext4 rw,relatime 0 0
overlay /home/docker/overlay2/2f58023e/merged overlay rw,relatime,lowerdir=/home/docker/overlay2/l/N:/home/docker/overlay2/l/A,upperdir=/home/docker/overlay2/2f58023e/diff,workdir=/home/docker/overlay2/2f58023e/work 0 0
overlay /run/k3s/containerd/io.containerd.runtime.v2.task/k8s.io/60f1/rootfs overlay rw,lowerdir=/var/lib/rancher/k3s/agent/containerd/snapshots/1/fs 0 0
"""


@pytest.fixture
def no_docker(monkeypatch, tmp_path):
    """A node whose docker socket this user cannot open, with nothing else readable."""
    monkeypatch.setattr(dp, "docker_root", lambda: "")
    monkeypatch.setattr(dp, "DAEMON_JSON", tmp_path / "daemon.json")
    monkeypatch.setattr(dp, "PROC_MOUNTS", tmp_path / "absent-mounts")
    monkeypatch.setattr(dp, "_k3d_mount_sources", lambda: [])
    return tmp_path


class TestDockerDataRoot:
    def test_docker_info_wins_when_it_answers(self, no_docker, monkeypatch):
        monkeypatch.setattr(dp, "docker_root", lambda: "/srv/docker")
        assert dp.resolve_docker_data_root().root == "/srv/docker"

    def test_the_daemon_config_names_the_data_root_without_the_socket(self, no_docker):
        assert not dp.DAEMON_JSON.exists()
        dp.DAEMON_JSON.write_text(json.dumps({"data-root": "/home/docker"}), encoding="utf-8")
        found = dp.resolve_docker_data_root()
        assert found.root == "/home/docker"
        assert "daemon.json" in found.via

    def test_running_containers_mounts_name_the_data_root(self, no_docker):
        dp.PROC_MOUNTS.write_text(PROC_MOUNTS, encoding="utf-8")
        found = dp.resolve_docker_data_root()
        assert found.root == "/home/docker"
        assert "mounts" in found.via

    def test_k3d_node_volume_sources_name_the_data_root(self, no_docker, monkeypatch):
        monkeypatch.setattr(dp, "_k3d_mount_sources", lambda: [
            "/home/docker/volumes/1a41/_data", "/home/docker/volumes/k3d-x-images/_data"])
        assert dp.resolve_docker_data_root().root == "/home/docker"

    def test_a_daemon_config_without_data_root_is_the_default_root(self, no_docker):
        dp.DAEMON_JSON.write_text("{}", encoding="utf-8")
        assert dp.resolve_docker_data_root().root == "/var/lib/docker"

    def test_nothing_readable_is_unresolved_and_says_what_was_tried(self, no_docker):
        found = dp.resolve_docker_data_root()
        assert found.root == ""
        said = " ".join(found.tried)
        for tried in ("docker info", "daemon.json", "mounts", "k3d"):
            assert tried in said

    def test_an_unresolved_auto_volume_stays_unresolved_never_slash(self, no_docker):
        spec = dp._resolve_auto(dl.VolumeSpec("rt", dp.AUTO_DOCKER_ROOT, ("nodefs",)))
        assert spec.path == dp.AUTO_DOCKER_ROOT
        assert spec.unresolved

    def test_no_config_with_docker_unresolved_does_not_measure_k3s_on_root(
        self, no_docker, monkeypatch
    ):
        monkeypatch.delenv(dp.CONFIG_ENV, raising=False)
        monkeypatch.setenv("AXI_STATE_DIR", str(no_docker / "state"))
        monkeypatch.setattr(dp.shutil, "which", lambda b: f"/usr/bin/{b}")
        cfg = dp.resolve_config()
        assert [v.name for v in cfg.volumes] == ["container-data-root"]
        assert cfg.volumes[0].unresolved
        assert all(v.path != "/var/lib/rancher/k3s" for v in cfg.volumes)


class _Probes:
    def __init__(self, free: dict[str, int]):
        self.free = free

    def usage(self, path):
        if path not in self.free:
            raise FileNotFoundError(2, "No such file or directory")
        return 3000 * GiB, self.free[path]

    def kubelet(self, cfg):
        return dl.KubeletView(policy=dl.EvictionPolicy(
            hard={"nodefs.available": "50Gi"}, source="configz"))


class TestUnresolvedAlerts:
    NOW = datetime(2026, 10, 31, 7, 0, tzinfo=UTC)

    def _cfg(self):
        return dl.LadderConfig(volumes=[
            dl.VolumeSpec("container-data-root", dp.AUTO_DOCKER_ROOT, ("nodefs",),
                          unresolved="tried docker info, daemon.json"),
            dl.VolumeSpec("home", "/home"),
        ])

    def test_an_unresolved_volume_is_named_unresolved_in_the_reading(self):
        rep = dl.assess(self._cfg(), _Probes({"/home": 2000 * GiB}), self.NOW)
        row = next(v for v in rep["volumes"] if v["name"] == "container-data-root")
        assert row["rung"] == "unresolved"
        assert "unresolved" in rep["summary"]

    def test_an_unresolved_volume_alerts_even_when_everything_measured_is_ok(self):
        rep = dl.assess(self._cfg(), _Probes({"/home": 2000 * GiB}), self.NOW)
        assert rep["rung"] == "warn"
        assert dl.should_notify(rep, None, dl.LadderThresholds(), self.NOW)

    def test_unresolved_alone_still_alerts(self):
        cfg = dl.LadderConfig(volumes=self._cfg().volumes[:1])
        rep = dl.assess(cfg, _Probes({}), self.NOW)
        assert rep["rung"] == "warn"


class TestUnreadablePath:
    def test_a_path_behind_a_closed_directory_is_measured_on_its_filesystem(
        self, monkeypatch, tmp_path
    ):
        mounts = tmp_path / "mounts"
        mounts.write_text(PROC_MOUNTS, encoding="utf-8")
        monkeypatch.setattr(dp, "PROC_MOUNTS", mounts)
        seen = []

        def usage(path):
            seen.append(path)
            if path != "/home":
                raise PermissionError(13, "Permission denied")
            return type("U", (), {"total": 10, "used": 4, "free": 6})()

        monkeypatch.setattr(dp.shutil, "disk_usage", usage)
        assert dp.NodeProbes().usage("/home/docker/volumes/1a41/_data/storage/pvc-x") == (10, 6)
        assert seen[-1] == "/home"

    def test_no_mount_table_keeps_the_permission_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dp, "PROC_MOUNTS", tmp_path / "absent")

        def usage(path):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(dp.shutil, "disk_usage", usage)
        with pytest.raises(PermissionError):
            dp.NodeProbes().usage("/home/docker/x")


class TestWhereTheConfigAndReadingLive:
    def test_a_hand_run_finds_the_config_the_deploy_installed(self, monkeypatch, tmp_path):
        """deploy_site.sh installs ``~/.axi/config/disk-ladder.toml``; the timer
        names it in its environment, a person typing the verb does not."""
        monkeypatch.delenv(dp.CONFIG_ENV, raising=False)
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        (tmp_path / "config").mkdir()
        site = tmp_path / "config" / "disk-ladder.toml"
        site.write_text('[[volume]]\nname = "data"\npath = "/home/docker"\n', encoding="utf-8")
        cfg = dp.resolve_config()
        assert cfg.source == str(site)
        assert [v.path for v in cfg.volumes] == ["/home/docker"]

    def test_a_volume_path_may_start_at_home(self, tmp_path):
        p = tmp_path / "l.toml"
        p.write_text('[[volume]]\nname = "bronze"\npath = "~/.axi/bronze"\n', encoding="utf-8")
        assert dl.load_config(p).volumes[0].path == str(Path("~/.axi/bronze").expanduser())

    def test_the_reading_is_machine_state_the_same_under_every_brand(self, monkeypatch, tmp_path):
        """The timer runs ``axi``; a person types ``neut status``. One file for both."""
        from axiom.infra import paths

        monkeypatch.delenv("AXI_STATE_DIR", raising=False)
        monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(paths, "project_dir_name", lambda: ".neut")
        assert dp.state_path() == tmp_path / ".axi" / "hygiene" / "disk-ladder.json"

    def test_the_timers_run_is_what_status_reads(self, monkeypatch, tmp_path):
        from axiom.extensions.builtins.hygiene.skills import disk
        from axiom.extensions.builtins.status.cli import HealthChecker, HealthStatus

        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        cfg_path = tmp_path / "ladder.toml"
        cfg_path.write_text(f'[ladder]\nrecipient = "@ops:local"\n[[volume]]\nname = "d"\n'
                            f'path = "{tmp_path}"\n', encoding="utf-8")
        monkeypatch.setattr(dp, "inbox_notifier", lambda r: lambda alert: None)
        monkeypatch.setattr(dp.NodeProbes, "kubelet",
                            lambda self, cfg: dl.KubeletView(policy=dl.EvictionPolicy.none()))
        from axiom.infra.skills import SkillContext, SkillRegistry

        ctx = SkillContext(registry=SkillRegistry(), state_dir=tmp_path,
                           logger=logging.getLogger("t"))
        assert disk.watch({"config": str(cfg_path)}, ctx).ok
        row = HealthChecker().check_disk_ladder()
        assert "not measured" not in row.message
        assert row.status != HealthStatus.UNKNOWN
