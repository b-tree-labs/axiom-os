# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``instrument video install``: the capture service as a unit the OS starts at boot.

A started source is a durable mark, so a capture service in ``--all`` mode
started at boot picks every started source back up. Without it, a reboot (or a
WSL restart) leaves every source started and nothing capturing it.

* ``install`` registers ``capture --all`` with the platform's service manager
  (systemd user unit, launchd, Task Scheduler), pinned to this state directory,
  and starts it. It refuses where the only provider is a bare subprocess, which
  does not survive a reboot, rather than reporting an install that is not one.
  A systemd user unit outlives a login only with linger, so that is reported
  with the command to turn it on.
* A running ``--all`` service advertises itself; ``start`` then hands it the
  source instead of spawning a process of its own.
* ``uninstall`` stops and removes the unit and leaves the started marks alone.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

from axiom.extensions.builtins.video import boot, control
from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video import state as vs
from axiom.extensions.builtins.video.control import SERVICE_MODULE
from axiom.extensions.builtins.video.services.capture import CaptureService

from .fakes import FOREVER, FakeDecoder
from .harness import wait_for


class FakeManager:
    """Stands where ``axiom.infra.services.ServiceManager`` stands."""

    def __init__(
        self, provider: str = "systemd", *, installs: bool = True, status: str = "running"
    ) -> None:
        self.provider = provider
        self.installs = installs
        self.state = status
        self.calls: list[str] = []
        self.made_with: dict = {}

    @property
    def provider_name(self) -> str:
        return self.provider

    def install(self) -> bool:
        self.calls.append("install")
        return self.installs

    def start(self) -> bool:
        self.calls.append("start")
        return True

    def stop(self) -> bool:
        self.calls.append("stop")
        return True

    def uninstall(self) -> bool:
        self.calls.append("uninstall")
        return True

    def status(self):
        from axiom.infra.services import ServiceInfo

        return ServiceInfo(name="video-capture", status=self.state, provider=self.provider)


@pytest.fixture
def manager(monkeypatch):
    made = FakeManager()

    def factory(**kwargs):
        made.made_with = kwargs
        return made

    monkeypatch.setattr(boot, "make_manager", factory)
    monkeypatch.setattr(boot, "linger_enabled", lambda: True)
    return made


# ---- what is registered --------------------------------------------------------------


def test_the_unit_runs_capture_all_on_this_interpreter_and_this_state_dir(state_dir, monkeypatch):
    monkeypatch.setenv("AXIOM_VIDEO_FFMPEG", "/opt/ffmpeg/bin/ffmpeg")
    spec = boot.service_definition(state_dir)
    assert spec["name"] == boot.SERVICE_NAME == "video-capture"
    assert spec["binary"] == sys.executable
    assert spec["args"] == ["-m", SERVICE_MODULE, "--all"]
    assert spec["env"]["AXI_STATE_DIR"] == str(state_dir)
    assert spec["env"]["AXIOM_VIDEO_FFMPEG"] == "/opt/ffmpeg/bin/ffmpeg", (
        "an ffmpeg override goes with it"
    )
    assert spec["interval_secs"] == 0, "a daemon, not a timer"


def test_the_unit_carries_no_credential(state_dir, login, monkeypatch):
    vc.connect(
        vc.VideoConnector(
            name="cam-1", credential_ref=login.ref, source={"kind": "rtsp", "url": "rtsp://h/ok"}
        ),
        state_dir=state_dir,
    )
    spec = boot.service_definition(state_dir)
    assert login.password not in repr(spec)
    assert not any(k.startswith("VIDEO_TEST_LOGIN") for k in spec["env"]), (
        "the environment is not copied wholesale"
    )


# ---- install / uninstall ---------------------------------------------------------------


def test_install_registers_and_starts_it(state_dir, call, manager):
    result = call("video.install")
    assert result.ok, result.errors
    assert manager.calls == ["install", "start"]
    assert manager.made_with["state_dir"] == state_dir
    value = result.value
    assert (
        value["installed"] is True
        and value["provider"] == "systemd"
        and value["survives_reboot"] is True
    )


def test_install_says_when_a_systemd_unit_will_not_outlive_the_login(
    state_dir, call, manager, monkeypatch
):
    monkeypatch.setattr(boot, "linger_enabled", lambda: False)
    result = call("video.install")
    assert result.ok and result.value["survives_reboot"] is False
    assert "loginctl enable-linger" in result.value["fix"]


def test_install_refuses_where_nothing_would_survive_a_reboot(state_dir, call, manager):
    manager.provider = "subprocess"
    result = call("video.install")
    assert not result.ok
    assert manager.calls == [], "nothing is registered"
    assert "reboot" in result.errors[0] and "systemd" in result.errors[0]


def test_a_failed_registration_is_a_failure(state_dir, call, manager):
    manager.installs = False
    result = call("video.install")
    assert not result.ok and "start" not in manager.calls


def test_uninstall_removes_the_unit_and_keeps_the_started_marks(state_dir, call, manager):
    vs.mark_started("cam-1", state_dir)
    result = call("video.uninstall")
    assert result.ok, result.errors
    assert manager.calls == ["stop", "uninstall"]
    assert vs.is_started("cam-1", state_dir)
    assert "cam-1" in " ".join(result.actions_taken + [result.value.get("note", "")])


def test_install_is_reversible_and_declares_it():
    from axiom.extensions.builtins.video.skills import SPECS

    specs = {s.name: s for s in SPECS}
    for name in ("video.install", "video.uninstall"):
        assert specs[name].risk == "reversible-write" and specs[name].requires is not None
        assert specs[name].surfaces == ("cli",)


# ---- a running service takes what start hands it -------------------------------------


@pytest.fixture
def all_service(state_dir, fake_source):
    svc = CaptureService(
        None, state_dir=state_dir, decoder=FakeDecoder(default=[FOREVER]), watch_s=0.05
    )
    svc.start()
    yield svc
    svc.stop(timeout=10)


def test_a_running_all_service_advertises_itself(state_dir, all_service):
    (advert,) = wait_for(lambda: boot.running_services(state_dir), timeout=5)
    assert advert["pid"] == os.getpid() and advert["mode"] == "all"
    all_service.stop(timeout=10)
    assert boot.running_services(state_dir) == []


def test_a_named_service_does_not_advertise(state_dir, fake_source):
    svc = CaptureService(
        ["cam-1"], state_dir=state_dir, decoder=FakeDecoder(default=[FOREVER]), watch_s=0.05
    )
    svc.start()
    try:
        assert boot.running_services(state_dir) == []
    finally:
        svc.stop(timeout=10)


def test_start_hands_the_source_to_the_running_service(state_dir, fake_ffmpeg, all_service):
    vc.connect(vc.VideoConnector(name="cam-1", source={"kind": "fake"}), state_dir=state_dir)
    assert vc.verify("cam-1", state_dir=state_dir).ok
    wait_for(lambda: boot.running_services(state_dir), timeout=5)

    def never(*a, **k):  # pragma: no cover - the assertion is that it is not called
        raise AssertionError("start spawned a process although a service was running")

    outcome = control.start("cam-1", state_dir=state_dir, spawn=never)
    assert outcome["pid"] == os.getpid() and outcome["via"] == "service"
    assert wait_for(
        lambda: (vs.read_live("cam-1", state_dir) or {}).get("state") == "running", timeout=10
    )


def test_without_a_service_start_spawns_as_before(state_dir, fake_ffmpeg, fake_source):
    vc.connect(vc.VideoConnector(name="cam-1", source={"kind": "fake"}), state_dir=state_dir)
    assert vc.verify("cam-1", state_dir=state_dir).ok
    spawned = []

    class Proc:
        pid = os.getpid()

        def poll(self):
            return None

    def spawn(name, *, state_dir):
        spawned.append(name)
        # stand in for the spawned process taking the claim
        sup_thread = threading.Thread(
            target=lambda: CaptureService(
                [name], state_dir=state_dir, decoder=FakeDecoder(default=[FOREVER]), watch_s=0.05
            ).run_forever(install_signals=False),
            daemon=True,
        )
        sup_thread.start()
        return Proc()

    outcome = control.start("cam-1", state_dir=state_dir, spawn=spawn)
    assert spawned == ["cam-1"] and outcome["via"] == "spawned"
    control.stop("cam-1", state_dir=state_dir)


def test_status_says_whether_capture_will_come_back_after_a_reboot(
    state_dir, call, manager, all_service
):
    wait_for(lambda: boot.running_services(state_dir), timeout=5)
    service = call("video.status").value["service"]
    assert service["installed"] is True and service["provider"] == "systemd"
    assert service["running"] and service["running"][0]["pid"] == os.getpid()


def test_status_without_a_service_says_so(state_dir, call, manager):
    from axiom.infra.services import ServiceStatus

    manager.state = ServiceStatus.NOT_INSTALLED
    service = call("video.status").value["service"]
    assert service["installed"] is False and service["running"] == []
    assert "instrument video install" in service["note"]
