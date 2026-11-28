# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The disk ladder against a (faked) node, and on every surface it serves.

The probes shell out to ``kubectl`` and ``docker``; here those answer from
canned JSON, so the parsing of a real ``configz``, a node's conditions and its
image list is exercised without a cluster.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.hygiene import disk_ladder as dl
from axiom.extensions.builtins.hygiene import disk_probes as dp

GiB = 2**30

NODE = {
    "items": [{
        "metadata": {"name": "k3d-dev-server-0"},
        "status": {
            "conditions": [{"type": "DiskPressure", "status": "False"}],
            "images": [{"names": ["docker.io/library/cache-image:2"]}],
        },
    }]
}
CONFIGZ = {"kubeletconfig": {
    "evictionHard": {"nodefs.available": "50Gi", "imagefs.available": "50Gi",
                     "memory.available": "100Mi"},
    "evictionMinimumReclaim": {"nodefs.available": "5Gi", "imagefs.available": "5Gi"},
}}
PODS = {"items": [
    {"spec": {"containers": [{"image": "db-image:1.0", "imagePullPolicy": "Never"},
                             {"image": "web:3", "imagePullPolicy": "IfNotPresent"}]}},
    {"spec": {"containers": [{"image": "cache-image:2", "imagePullPolicy": "Never"}]}},
]}


@pytest.fixture
def fake_node(monkeypatch):
    calls: list[list[str]] = []

    def run(argv):
        calls.append(argv)
        if argv[0] == "kubectl":
            if "nodes" in argv:
                return 0, json.dumps(NODE)
            if "--raw" in argv:
                return 0, json.dumps(CONFIGZ)
            if "pods" in argv:
                return 0, json.dumps(PODS)
        if argv[:3] == ["docker", "image", "inspect"]:
            return (0, "sha256:x") if argv[-1] == "db-image:1.0" else (1, "")
        return 1, ""

    monkeypatch.setattr(dp, "_run", run)
    monkeypatch.setattr(dp.shutil, "which", lambda b: f"/usr/bin/{b}")
    return calls


class TestNodeProbes:
    def test_reads_the_running_kubelets_thresholds_through_configz(self, fake_node):
        view = dp.NodeProbes().kubelet(dl.LadderConfig())
        assert view.policy.source == "configz"
        assert view.policy.floor_bytes(("nodefs",), 10**12) == 50 * GiB
        assert view.policy.reclaim_bytes(("imagefs",), 10**12) == 5 * GiB
        assert view.node == "k3d-dev-server-0"
        assert view.pressure_on is False
        raw = [c for c in fake_node if "--raw" in c][0]
        assert raw[-1] == "/api/v1/nodes/k3d-dev-server-0/proxy/configz"

    def test_finds_never_pull_images_and_whether_the_node_still_has_them(self, fake_node):
        view = dp.NodeProbes().kubelet(dl.LadderConfig())
        by = {i.image: i for i in view.images}
        assert set(by) == {"db-image:1.0", "cache-image:2"}  # IfNotPresent is not a risk
        assert by["cache-image:2"].on_node is True  # docker.io/library/ spelling matches
        assert by["db-image:1.0"].on_node is False
        assert by["db-image:1.0"].on_host is True

    def test_the_kubectl_context_is_passed_when_configured(self, fake_node):
        dp.NodeProbes().kubelet(dl.LadderConfig(kubectl_context="k3d-dev"))
        assert all(c[1:3] == ["--context", "k3d-dev"] for c in fake_node if c[0] == "kubectl")

    def test_an_api_that_does_not_answer_leaves_the_policy_unread(self, monkeypatch):
        monkeypatch.setattr(dp, "_run", lambda argv: (1, ""))
        monkeypatch.setattr(dp.shutil, "which", lambda b: f"/usr/bin/{b}")
        view = dp.NodeProbes().kubelet(dl.LadderConfig())
        assert view.policy.source == "unread"
        assert view.findings

    def test_no_kubectl_and_no_cluster_means_no_kubelet(self, monkeypatch):
        monkeypatch.setattr(dp.shutil, "which", lambda b: None)
        view = dp.NodeProbes().kubelet(dl.LadderConfig())
        assert view.policy.source == "none"
        assert not view.findings


class TestConfigResolution:
    def test_an_explicit_missing_path_is_an_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            dp.resolve_config(str(tmp_path / "absent.toml"))

    def test_the_environment_names_the_site_config(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "ladder.toml"
        cfg_path.write_text('[[volume]]\nname = "v"\npath = "/"\n', encoding="utf-8")
        monkeypatch.setenv(dp.CONFIG_ENV, str(cfg_path))
        cfg = dp.resolve_config()
        assert [v.name for v in cfg.volumes] == ["v"]
        assert cfg.source == str(cfg_path)

    def test_a_volume_can_name_the_runtime_data_root_instead_of_a_path(self, tmp_path,
                                                                     monkeypatch):
        cfg_path = tmp_path / "ladder.toml"
        cfg_path.write_text('[[volume]]\nname = "rt"\npath = "auto:docker-data-root"\n'
                            'signals = ["nodefs"]\n', encoding="utf-8")
        monkeypatch.setattr(dp, "docker_root", lambda: "/srv/docker")
        assert dp.resolve_config(str(cfg_path)).volumes[0].path == "/srv/docker"
        monkeypatch.setattr(dp, "docker_root", lambda: "")
        monkeypatch.setattr(dp, "DAEMON_JSON", tmp_path / "no-daemon.json")
        monkeypatch.setattr(dp, "PROC_MOUNTS", tmp_path / "no-mounts")
        monkeypatch.setattr(dp, "_k3d_mount_sources", lambda: [])
        # Unresolvable: kept as written and marked, so the reading says so and alerts.
        vol = dp.resolve_config(str(cfg_path)).volumes[0]
        assert vol.path == "auto:docker-data-root" and vol.unresolved

    def test_no_config_watches_the_runtime_data_root(self, monkeypatch, tmp_path):
        monkeypatch.delenv(dp.CONFIG_ENV, raising=False)
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(dp, "docker_root", lambda: "/srv/docker")
        monkeypatch.setattr(dp.shutil, "which", lambda b: f"/usr/bin/{b}")
        cfg = dp.resolve_config()
        assert cfg.volumes == [dl.VolumeSpec("container-data-root", "/srv/docker",
                                             ("nodefs", "imagefs"))]


def _ctx(tmp_path):
    from axiom.infra.skills import SkillContext, SkillRegistry

    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path,
                        logger=logging.getLogger("t"))


class TestSurfaces:
    def test_the_read_is_a_declared_read_only_capability_on_mcp(self):
        from axiom.extensions.builtins.hygiene import skills
        from axiom.infra.skills import SkillRegistry

        reg = SkillRegistry()
        skills.bind(reg)
        spec = reg.spec("hygiene.stat")
        assert "mcp" in spec.surfaces and spec.side_effects is False
        watch = reg.spec("hygiene.disk_watch")
        assert "mcp" not in (watch.surfaces or ())

    def test_stat_disk_returns_a_structured_reading(self, tmp_path, monkeypatch, fake_node):
        from axiom.extensions.builtins.hygiene import skills

        cfg_path = tmp_path / "ladder.toml"
        cfg_path.write_text(
            f'[[volume]]\nname = "data-root"\npath = "{tmp_path}"\nsignals = ["nodefs"]\n',
            encoding="utf-8",
        )
        res = skills.stat({"resource": "disk", "config": str(cfg_path)}, _ctx(tmp_path))
        assert res.ok, res.errors
        assert res.value["volumes"][0]["name"] == "data-root"
        assert res.value["kubelet"]["source"] == "configz"
        assert res.value["rung"] in dl.RUNGS
        assert any(line.startswith("disk: ") for line in res.actions_taken)

    def test_the_cli_notify_form_runs_the_watch_and_saves_for_status(
        self, tmp_path, monkeypatch, fake_node, capsys
    ):
        from axiom.extensions.builtins.hygiene import cli

        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        cfg_path = tmp_path / "ladder.toml"
        cfg_path.write_text(
            f'[ladder]\nrecipient = "@ops:local"\n'
            f'[[volume]]\nname = "data-root"\npath = "{tmp_path}"\n',
            encoding="utf-8",
        )
        sent = []
        monkeypatch.setattr(dp, "inbox_notifier", lambda r: sent.append)
        # Plenty of room, whatever the machine running this has: below the
        # floor plus margin the rung is critical and outranks the missing image.
        monkeypatch.setattr(dp.NodeProbes, "usage", lambda self, path: (1000 * GiB, 500 * GiB))
        rc = cli.main(["stat", "disk", "--config", str(cfg_path), "--notify", "--json"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["volumes"][0]["name"] == "data-root"
        assert dp.state_path().is_file()
        # db-image:1.0 is gone from the node: that pages, and the page went out.
        assert out["rung"] == "page"
        assert sent and sent[0]["recipient"] == "@ops:local"

    def test_status_shows_the_saved_rung_in_one_line(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.status.cli import HealthStatus, HealthChecker

        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        row = HealthChecker().check_disk_ladder()
        assert row.status == HealthStatus.UNKNOWN and "not measured" in row.message

        class P:
            def usage(self, path):
                return 3 * 10**12, 120 * 10**9

            def kubelet(self, cfg):
                return dl.KubeletView(policy=dl.EvictionPolicy.none())

        cfg = dl.LadderConfig(volumes=[dl.VolumeSpec("bronze", "/srv/bronze")])
        dl.watch(cfg, probes=P(), notify=lambda _: None, state_path=dp.state_path(),
                 now=datetime.now(UTC) - timedelta(minutes=1))
        row = HealthChecker().check_disk_ladder()
        assert row.status == HealthStatus.DEGRADED
        assert row.message.startswith("warn") and "\n" not in row.message
