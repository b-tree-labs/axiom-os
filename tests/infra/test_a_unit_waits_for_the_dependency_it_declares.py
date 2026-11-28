# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A unit that depends on a runtime waits for it, and the runtime says how.

Three user units failed at a site node's boot on 2026-10-15 because they
started before the runtimes they need were answering. Each had been written
by hand against one runtime; the one that did poll had no timeout, so a
runtime that never came up gave a unit that hung rather than one that said so;
and none was ordered after the units it needed.

Readiness is a property of the *dependency*, declared once in the node's own
declaration (ADR-164, ``node.toml``) as one probe — an HTTP URL, a TCP
address, a command, or a file. A service that names it in ``depends_on`` gets
its gate generated from that declaration: an ``ExecStartPre`` that waits with
a timeout and fails loudly, ``Restart=on-failure`` with backoff and no
start-limit lockout, and ``After=`` ordering among the node's own units.

Nothing here names a real runtime. Adding one is a declaration, not code.
"""

from __future__ import annotations

import http.server
import socket
import subprocess
import sys
import threading
from unittest.mock import patch

import pytest

from axiom.infra import readiness
from axiom.infra.services import ServiceDef, SystemdProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def node_toml(tmp_path, monkeypatch):
    """The node's declaration, redirected so no real node.toml is touched."""
    path = tmp_path / "node.toml"
    monkeypatch.setenv("AXIOM_NODE_CONFIG", str(path))
    return path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Clock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


class TestADependencyDeclaresItsOwnReadiness:
    def test_each_probe_kind_is_read_from_the_declaration(self):
        deps = {
            "answers": {"http": "http://127.0.0.1:1/ready"},
            "listens": {"tcp": "127.0.0.1:1", "timeout": 120},
            "reports": {"command": ["true"]},
            "arrives": {"file": "/tmp/x/.ready", "interval": 0.5},
        }
        parsed = {name: readiness.dependency(name, spec) for name, spec in deps.items()}
        assert parsed["answers"].kind == "http"
        assert parsed["listens"].kind == "tcp" and parsed["listens"].timeout == 120
        assert parsed["reports"].kind == "command" and parsed["reports"].target == ("true",)
        assert parsed["arrives"].kind == "file" and parsed["arrives"].interval == 0.5
        # The defaults are the dependency's, not the caller's guess.
        assert parsed["answers"].timeout == readiness.DEFAULT_TIMEOUT

    def test_a_dependency_declares_exactly_one_probe(self):
        with pytest.raises(ValueError, match="exactly one"):
            readiness.dependency("mute", {"timeout": 10})
        with pytest.raises(ValueError, match="exactly one"):
            readiness.dependency("twice", {"http": "http://127.0.0.1:1/", "tcp": "127.0.0.1:1"})

    def test_a_malformed_probe_is_refused_by_name(self):
        with pytest.raises(ValueError, match="listens"):
            readiness.dependency("listens", {"tcp": "no-port-here"})
        with pytest.raises(ValueError, match="answers"):
            readiness.dependency("answers", {"http": "ftp://127.0.0.1/"})
        with pytest.raises(ValueError, match="name"):
            readiness.dependency("has space", {"file": "/x"})

    def test_declare_writes_the_node_file_and_keeps_what_was_there(self, node_toml):
        node_toml.write_text('[node]\nrole = "collector"\nfunctions = ["acquire", "transmit"]\n')
        dep = readiness.declare("warm-cache", http="http://127.0.0.1:1/ready", timeout=45)
        assert dep.kind == "http" and dep.timeout == 45
        again = readiness.dependencies()
        assert set(again) == {"warm-cache"}
        assert again["warm-cache"].target == "http://127.0.0.1:1/ready"
        from axiom.infra import node_functions

        cfg = node_functions.load()
        assert cfg.functions == ("acquire", "transmit"), "declaring a dependency lost the role"
        # Declaring a second one keeps the first; undeclaring removes only its own.
        readiness.declare("store", tcp="127.0.0.1:1")
        assert set(readiness.dependencies()) == {"warm-cache", "store"}
        readiness.undeclare("warm-cache")
        assert set(readiness.dependencies()) == {"store"}

    def test_the_node_cli_noun_shows_on_a_confined_node(self):
        """A gate that is hidden on the nodes that boot unattended is no gate."""
        from axiom.infra.node_functions import ALWAYS_VISIBLE

        assert "node" in ALWAYS_VISIBLE


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------


class TestWaitingForADeclaredDependency:
    def test_a_dependency_that_becomes_ready_late_is_waited_for(self):
        dep = readiness.dependency("late", {"file": "/nowhere", "timeout": 60, "interval": 2})
        clock = _Clock()
        answers = iter([(False, "absent"), (False, "absent"), (True, "")])
        waited = readiness.wait_for(
            dep, clock=clock, sleep=clock.sleep, probe=lambda d: next(answers)
        )
        assert waited == pytest.approx(4.0)
        assert clock.sleeps == [2, 2]

    def test_a_dependency_that_never_becomes_ready_fails_loudly(self):
        dep = readiness.dependency("never", {"tcp": "127.0.0.1:9", "timeout": 10, "interval": 3})
        clock = _Clock()
        calls = 0

        def probe(d):
            nonlocal calls
            calls += 1
            return False, "connection refused"

        with pytest.raises(readiness.NotReady) as info:
            readiness.wait_for(dep, clock=clock, sleep=clock.sleep, probe=probe)
        msg = str(info.value)
        assert "never" in msg and "tcp 127.0.0.1:9" in msg
        assert "not ready after 10s" in msg and "connection refused" in msg
        assert info.value.waited == pytest.approx(clock.now)
        assert calls == 5, "stopped polling early or never stopped"

    def test_the_caller_may_shorten_or_lengthen_the_wait(self):
        dep = readiness.dependency("slow", {"file": "/nowhere", "timeout": 100, "interval": 10})
        clock = _Clock()
        with pytest.raises(readiness.NotReady) as info:
            readiness.wait_for(
                dep, timeout=25, clock=clock, sleep=clock.sleep, probe=lambda d: (False, "no")
            )
        assert "after 25s" in str(info.value)

    def test_the_http_probe_reads_the_real_answer(self):
        hits = {"n": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits["n"] += 1
                self.send_response(503 if hits["n"] < 3 else 200)
                self.end_headers()

            def log_message(self, *a):  # quiet
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            dep = readiness.dependency(
                "answers", {"http": f"http://127.0.0.1:{port}/ready", "interval": 0.02}
            )
            assert readiness.wait_for(dep, timeout=5) >= 0
            assert hits["n"] == 3
        finally:
            server.shutdown()
        dead = readiness.dependency(
            "dead", {"http": f"http://127.0.0.1:{_free_port()}/ready", "interval": 0.02}
        )
        with pytest.raises(readiness.NotReady, match="dead"):
            readiness.wait_for(dead, timeout=0.2)

    def test_the_tcp_probe_needs_an_accepted_connection(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            dep = readiness.dependency("listens", {"tcp": f"127.0.0.1:{port}"})
            ready, _ = readiness.probe(dep)
            assert ready
        closed = readiness.dependency("closed", {"tcp": f"127.0.0.1:{_free_port()}"})
        ready, detail = readiness.probe(closed)
        assert not ready and detail

    def test_the_command_probe_reads_the_exit_code(self):
        ok = readiness.dependency("ok", {"command": [sys.executable, "-c", "raise SystemExit(0)"]})
        assert readiness.probe(ok) == (True, "")
        bad = readiness.dependency(
            "bad",
            {
                "command": [
                    sys.executable,
                    "-c",
                    "import sys; print('nope', file=sys.stderr); sys.exit(3)",
                ]
            },
        )
        ready, detail = readiness.probe(bad)
        assert not ready and "exit 3" in detail and "nope" in detail
        missing = readiness.dependency("missing", {"command": "/no/such/binary --flag"})
        ready, detail = readiness.probe(missing)
        assert not ready and "not found" in detail

    def test_the_file_probe(self, tmp_path):
        marker = tmp_path / ".ready"
        dep = readiness.dependency("arrives", {"file": str(marker)})
        assert readiness.probe(dep)[0] is False
        marker.write_text("")
        assert readiness.probe(dep)[0] is True


# ---------------------------------------------------------------------------
# The generated unit
# ---------------------------------------------------------------------------


@pytest.fixture
def provider(tmp_path, monkeypatch):
    prov = SystemdProvider()
    monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path / "units")
    monkeypatch.setattr(prov, "_linger_enabled", lambda: True)
    monkeypatch.setattr("axiom.infra.services.get_user_state_dir", lambda: tmp_path / "state")
    return prov


def _install(prov, svc) -> bool:
    with patch("axiom.infra.services.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        return prov.install(svc)


def _svc(**kw) -> ServiceDef:
    base = dict(name="feeder", binary="/bin/sh", args=["-c", "true"], env={})
    base.update(kw)
    return ServiceDef(**base)


TWO_DEPS = (
    "[dependencies.warm-cache]\n"
    'http = "http://127.0.0.1:1/ready"\n'
    "timeout = 120\n\n"
    "[dependencies.store]\n"
    'tcp = "127.0.0.1:1"\n'
)


class TestTheGeneratedUnitCarriesTheGate:
    def test_a_daemon_that_depends_on_runtimes_waits_for_each_then_retries_without_lockout(
        self, provider, node_toml, tmp_path
    ):
        node_toml.write_text(TWO_DEPS)
        svc = _svc(depends_on=("warm-cache", "store"), after=("neut-store.service",))
        assert _install(provider, svc) is True
        unit = (tmp_path / "units" / "neut-feeder.service").read_text()

        gates = [ln for ln in unit.splitlines() if ln.startswith("ExecStartPre=")]
        assert len(gates) == 2
        assert gates[0].endswith("node wait-for warm-cache --timeout 120"), gates[0]
        assert gates[1].endswith("node wait-for store --timeout 300"), gates[1]
        assert unit.index("ExecStartPre=") < unit.index("ExecStart="), "the gate must run first"
        # Ordered after the node's own units, not only after the network.
        assert "After=network.target neut-store.service" in unit
        # Retries with backoff, and never locks itself out while a runtime is late.
        assert "Restart=on-failure" in unit
        assert "StartLimitIntervalSec=0" in unit and "StartLimitBurst" not in unit
        assert "RestartSec=" in unit and "RestartMaxDelaySec=" in unit

    def test_a_periodic_unit_gets_the_same_gate(self, provider, node_toml, tmp_path):
        node_toml.write_text(TWO_DEPS)
        svc = _svc(name="sync", depends_on=("store",), interval_secs=300)
        assert _install(provider, svc) is True
        unit = (tmp_path / "units" / "neut-sync.service").read_text()
        assert "Type=oneshot" in unit
        assert "node wait-for store --timeout 300" in unit
        assert "Restart=on-failure" in unit and "StartLimitIntervalSec=0" in unit
        assert "After=network.target" in unit

    def test_the_service_may_set_its_own_patience(self, provider, node_toml, tmp_path):
        node_toml.write_text(TWO_DEPS)
        svc = _svc(depends_on=("warm-cache",), ready_timeout_secs=30)
        assert _install(provider, svc) is True
        unit = (tmp_path / "units" / "neut-feeder.service").read_text()
        assert "node wait-for warm-cache --timeout 30" in unit

    def test_a_unit_without_dependencies_is_rendered_as_before(self, provider, node_toml, tmp_path):
        node_toml.write_text(TWO_DEPS)
        assert _install(provider, _svc()) is True
        daemon = (tmp_path / "units" / "neut-feeder.service").read_text()
        assert "ExecStartPre=" not in daemon
        assert "StartLimitBurst=5" in daemon and "RestartMaxDelaySec" not in daemon
        assert _install(provider, _svc(name="tick", interval_secs=60)) is True
        periodic = (tmp_path / "units" / "neut-tick.service").read_text()
        assert "ExecStartPre=" not in periodic and "Restart=" not in periodic

    def test_an_undeclared_dependency_refuses_the_install(
        self, provider, node_toml, tmp_path, caplog
    ):
        """A unit that waits for a name nobody declared would fail at every boot."""
        node_toml.write_text(TWO_DEPS)
        svc = _svc(depends_on=("ghost",))
        with caplog.at_level("ERROR"):
            assert _install(provider, svc) is False
        assert not (tmp_path / "units" / "neut-feeder.service").exists()
        assert "ghost" in caplog.text and "node dependency declare ghost" in caplog.text

    def test_the_gate_is_the_platform_verb(self, provider, node_toml, tmp_path):
        """The unit invokes the same verb an operator would type, with an absolute path."""
        node_toml.write_text(TWO_DEPS)
        assert _install(provider, _svc(depends_on=("store",))) is True
        unit = (tmp_path / "units" / "neut-feeder.service").read_text()
        gate = next(ln for ln in unit.splitlines() if ln.startswith("ExecStartPre="))
        command = gate.removeprefix("ExecStartPre=")
        assert command.startswith("/"), command
        assert " node wait-for store " in command
