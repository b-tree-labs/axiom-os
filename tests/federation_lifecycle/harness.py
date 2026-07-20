# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federation lifecycle test harness.

A small, explicit wrapper around ``docker compose`` that scenario tests call
to spin up a multi-node Axiom federation on the developer laptop, drive it
through lifecycle operations, and assert outcomes.

Design notes
------------
* Pure Python + docker compose. No Kubernetes, no Ansible, no custom
  orchestration layer. The goal is that a dev with Docker Desktop can
  ``python -m pytest tests/federation_lifecycle -v`` and see it work.
* Stateless. The harness holds no state across invocations: every method
  shells out to ``docker compose`` or ``docker exec``. This makes it safe
  to call from many scenarios (including parallel xdist workers, later)
  by parametrizing the compose project name.
* Capable, not clever. The surface is minimal: ``start``, ``stop``,
  ``exec``, ``add_peer``, ``assert_federated``, ``teardown``. Scenarios
  build on these primitives directly.

Scale horizon
-------------
The 16-scenario matrix in ``docs/prds/prd-federation.md §17`` — including
key rotation, Sybil containment, cross-root bridging, and 10k-node
simulation — is OUT of scope for this prototype. But the shape here
(addressing nodes by string name, identity-bound assertions, composable
lifecycle verbs) is intended to extend there without rewrite. For the
10k-scale phase, swap the compose backend for a lighter process-per-node
or testcontainers backend behind the same interface.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
COMPOSE_FILE = HARNESS_DIR / "docker-compose.yml"
REPO_ROOT = HARNESS_DIR.parent.parent


class HarnessError(RuntimeError):
    """Raised when a harness operation fails in a way the test cares about."""


def docker_available() -> tuple[bool, str]:
    """Return (ok, reason). Used by pytest skip guards."""
    if shutil.which("docker") is None:
        return False, "docker CLI not on PATH"
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"docker not responsive: {exc}"
    if r.returncode != 0:
        return False, f"docker daemon unreachable: {r.stderr.strip() or r.stdout.strip()}"
    # Compose v2 plugin check
    r2 = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if r2.returncode != 0:
        return False, "docker compose v2 plugin not installed"
    return True, ""


@dataclass
class FederationHarness:
    """Orchestrates a compose-backed federation for a single scenario.

    Use as a context manager so teardown always runs::

        with FederationHarness(project="happy3") as fed:
            fed.start()
            fed.exec("hub", "axi federation init --owner test@x --name hub")
            ...
    """

    project: str = "axifed"
    compose_file: Path = COMPOSE_FILE
    nodes: tuple[str, ...] = ("hub", "leaf1", "leaf2")
    wait_timeout_s: int = 60
    _started: bool = field(default=False, init=False, repr=False)

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> FederationHarness:
        return self

    def __exit__(self, *exc) -> None:
        # Always teardown, even on assertion failure. Don't swallow exceptions.
        try:
            self.teardown()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass

    def start(self) -> None:
        """Bring up all nodes, generate a shared test keypair, wait for sshd."""
        self._seed_keys()
        self._compose("up", "-d", "--build")
        self._started = True
        self._wait_for_sshd()

    def stop(self) -> None:
        """Stop containers but keep volumes/keys (for debugging between tests)."""
        self._compose("stop")

    def teardown(self) -> None:
        """Stop and remove everything — containers, networks, volumes."""
        if not Path(self.compose_file).exists():
            return
        self._compose("down", "-v", "--remove-orphans", check=False)
        self._started = False

    # -- primitives ---------------------------------------------------------

    def exec(
        self,
        node: str,
        cmd: str,
        *,
        user: str = "axiom",
        check: bool = True,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess:
        """Run a shell command inside ``node`` as ``user``.

        Uses ``docker compose exec -T`` (no TTY) so output is captureable.
        ``cmd`` is run through ``bash -lc`` so PATH/profile expansions work
        the same way they would for an interactive operator.
        """
        args = [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            "-p",
            self.project,
            "exec",
            "-T",
            "--user",
            user,
            node,
            "bash",
            "-lc",
            cmd,
        ]
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise HarnessError(
                f"exec failed on {node}: `{cmd}`\n"
                f"  rc={r.returncode}\n  stdout={r.stdout}\n  stderr={r.stderr}"
            )
        return r

    def exec_json(self, node: str, cmd: str, **kw) -> dict:
        """Run a command that emits JSON on stdout and return the parsed dict."""
        r = self.exec(node, cmd, **kw)
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            raise HarnessError(f"node {node} returned non-JSON for `{cmd}`:\n{r.stdout!r}") from exc

    def add_peer(self, from_node: str, to_node: str) -> dict:
        """Register ``to_node`` as a peer on ``from_node`` via SSH.

        This is the identity-binding path: `axi nodes add` → SSH to the peer
        → `axi federation status --json` → bind pubkey → state=VERIFIED.
        Returns the parsed JSON payload from the CLI.
        """
        ssh_target = f"axiom@{to_node}"
        cmd = f"axi nodes add {to_node} {ssh_target} --json"
        return self.exec_json(from_node, cmd, check=False)

    def assert_federated(self, node_a: str, node_b: str) -> None:
        """Assert that ``node_a`` has identity-bound ``node_b`` as a peer.

        Checks the three things that matter per ADR identity-binding:
          1. A peer entry exists on ``node_a`` whose transport points at
             ``node_b`` (display_name OR ssh_host).
          2. That entry's state is 'verified'.
          3. The stored public_key fingerprint matches the fingerprint
             ``node_b`` reports from its own `axi federation status`.
        """
        peers = self.exec_json(node_a, "axi federation peers --json")
        if isinstance(peers, dict):
            peers = peers.get("peers", [])
        matches = [p for p in peers if p.get("display_name") == node_b]
        if not matches:
            raise AssertionError(
                f"{node_a} has no peer matching display_name={node_b}; peers={peers}"
            )
        peer = matches[0]
        if peer.get("state") != "verified":
            raise AssertionError(
                f"{node_a}→{node_b} peer state is {peer.get('state')!r}, expected 'verified'"
            )

        # Cross-check fingerprints via the peer's own status
        own_status = self.exec_json(node_b, "axi federation status --json")
        from axiom.vega.federation.identity import fingerprint

        expected_fp = fingerprint(own_status["public_key"])
        # NodeRegistry stores fingerprint on the node record; peers listing
        # may or may not include it depending on version. Pull it from the
        # raw registry if needed.
        got_fp = peer.get("fingerprint")
        if got_fp is None:
            raw = self.exec(node_a, "cat ~/.axi/nodes.yaml || true", check=False).stdout
            if expected_fp not in raw:
                raise AssertionError(
                    f"fingerprint {expected_fp} (as reported by {node_b}) "
                    f"not found in {node_a}'s registry:\n{raw}"
                )
        elif got_fp != expected_fp:
            raise AssertionError(
                f"fingerprint mismatch: {node_a} stored {got_fp!r}, "
                f"{node_b} reports {expected_fp!r}"
            )

    # -- internals ----------------------------------------------------------

    def _compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        full = [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            "-p",
            self.project,
            *args,
        ]
        r = subprocess.run(full, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise HarnessError(
                f"docker compose {' '.join(args)} failed:\n  stdout={r.stdout}\n  stderr={r.stderr}"
            )
        return r

    def _seed_keys(self) -> None:
        """Generate an Ed25519 keypair on the host and copy it into the
        ``harness_keys`` volume so every node container can read it."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            priv = tdp / "id_ed25519"
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(priv), "-q"],
                check=True,
            )
            pub = tdp / "id_ed25519.pub"
            # Create the volume (no-op if it exists), then seed with a
            # throwaway helper container that mounts the volume + the keys.
            vol_name = f"{self.project}_harness_keys"
            subprocess.run(
                ["docker", "volume", "create", vol_name],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{vol_name}:/keys",
                    "-v",
                    f"{tdp}:/src:ro",
                    "alpine:3.19",
                    "sh",
                    "-c",
                    "cp /src/id_ed25519 /src/id_ed25519.pub /keys/ && "
                    "chmod 600 /keys/id_ed25519 && chmod 644 /keys/id_ed25519.pub",
                ],
                check=True,
                capture_output=True,
            )
            _ = pub  # referenced for clarity

    def _wait_for_sshd(self) -> None:
        deadline = time.time() + self.wait_timeout_s
        for node in self.nodes:
            while time.time() < deadline:
                r = self.exec(
                    node,
                    "true",
                    check=False,
                    timeout=5,
                )
                if r.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise HarnessError(f"timeout waiting for {node} to become responsive")
