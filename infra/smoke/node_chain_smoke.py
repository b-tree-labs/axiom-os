"""Prove one chart runs the same chain on Compose and on Kubernetes (K3D).

edge + producer from infra/charts/axiom-node; the downstream pull runs from the
host over the edge's loopback-published port (Compose) or a port-forward
(Kubernetes), exactly as a real downstream pulls over HTTPS. Keys are issued on
the host: the edge only ever receives hashes; tokens reach containers as an env
file or a Secret, never in a manifest.

Usage: node_chain_smoke.py compose|k3d   (needs helm, docker; k3d for k3d)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CHART = ROOT / "infra" / "charts" / "axiom-node"
AXI = os.environ.get("AXI", "axi")
EXPECT = 4 * 25


def sh(*a, **kw):
    kw.setdefault("check", True)
    return subprocess.run(list(a), text=True, capture_output=True, **kw)


def axi(env, *a):
    r = sh(AXI, *a, env=env, check=False)
    if r.returncode:
        raise SystemExit(
            f"`axi {' '.join(a[:3])}` failed ({r.returncode}):\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}"
        )
    return json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}


def issue(work: Path) -> tuple[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AXIOM_", "AXI_"))}
    env.update(
        AXI_STATE_DIR=str(work / "issuer"),
        AXIOM_GATE_API_KEYS_FILE=str(work / "edge" / "gate-api-keys.json"),
        AXI_DIAGNOSES_QUIET="1",
        AXIOM_ACTOR="@smoke:org",
    )
    prod = axi(
        env,
        "gate",
        "--json",
        "issue",
        "api-key",
        "--principal",
        "@daq:smoke-site",
        "--site",
        "smoke-site",
        "--scope",
        "data_platform:invoke",
    )["value"]["token"]
    down = axi(
        env,
        "gate",
        "--json",
        "issue",
        "api-key",
        "--principal",
        "@downstream:org",
        "--scope",
        "edge_export:read",
    )["value"]["token"]
    (work / "edge" / "sources.txt").write_text("smoke-src=smoke-site\n")
    assert prod not in (work / "edge" / "gate-api-keys.json").read_text(), (
        "the edge must hold hashes only"
    )
    return prod, down


def _down_env(work: Path, down: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AXIOM_", "AXI_"))}
    env.update(
        AXI_STATE_DIR=str(work / "down"),
        EDGE_TOK=down,
        AXI_DIAGNOSES_QUIET="1",
        AXIOM_ACTOR="@smoke:org",
    )
    return env


def pull_again(work: Path, down: str) -> dict:
    time.sleep(10)
    return axi(_down_env(work, down), "data", "--json", "edge-pull", "the-edge")["value"]


def free_port() -> int:
    """A port nothing else holds: a fixed one collides with whatever else runs on the machine."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FORWARD_PORT = free_port()


def _reforward():
    """Port-forward to the edge's front and wait until it answers."""
    import urllib.request

    pf = subprocess.Popen(
        ["kubectl", "port-forward", "svc/node-edge", f"{FORWARD_PORT}:8787"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def answers():
        try:
            return (
                urllib.request.urlopen(f"http://127.0.0.1:{FORWARD_PORT}/healthz", timeout=3).status
                == 200
            )
        except OSError:
            return False

    wait_for(answers, "the port-forward to the edge", timeout=120)
    return pf


def pull(work: Path, url: str, down: str) -> list[dict]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AXIOM_", "AXI_"))}
    env.update(
        AXI_STATE_DIR=str(work / "down"),
        EDGE_TOK=down,
        AXI_DIAGNOSES_QUIET="1",
        AXIOM_ACTOR="@smoke:org",
    )
    axi(
        env,
        "data",
        "register",
        "--bronze-root",
        str(work / "down-bronze" / "src"),
        "--site",
        "smoke-site",
        "--default-disposition",
        "allow",
        "--default-tier",
        "restricted",
        "smoke-src",
        "push",
    )
    axi(
        env,
        "data",
        "register",
        "--bronze-root",
        str(work / "down-bronze" / "edge"),
        "--credential-ref",
        "env://EDGE_TOK",
        "the-edge",
        "edge",
        "--edge-url",
        url,
    )
    return [axi(env, "data", "--json", "edge-pull", "the-edge")["value"] for _ in range(2)]


def wait_for(fn, what, timeout=600):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return
        time.sleep(5)
    raise SystemExit(f"timed out waiting for {what}")


def compose(work: Path, prod: str) -> tuple[str, callable]:
    (work / "config" / "producer").mkdir(parents=True)
    shutil.copy(HERE / "push.py", work / "config" / "producer" / "push.py")
    shutil.copytree(work / "config" / "producer", work / "config" / "twin")
    shutil.copy(HERE / "twin.py", work / "config" / "twin" / "twin.py")
    shutil.copytree(work / "edge", work / "config" / "edge")
    (work / "secrets").mkdir()
    (work / "secrets" / "producer.env").write_text(f"PRODUCER_TOKEN={prod}\n")
    for p in (work / "config", work / "secrets"):
        sh("chmod", "-R", "a+rX", str(p))
    sh(
        sys.executable,
        "-m",
        "axiom.infra.deploy.compose_from_chart",
        "--chart",
        str(CHART),
        "-f",
        str(HERE / "values-smoke.yaml"),
        "-o",
        str(work / "compose.yaml"),
        cwd=work,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    sh("docker", "compose", "-p", "nodesmoke", "up", "-d", cwd=work)

    def logs():
        return sh(
            "docker", "compose", "-p", "nodesmoke", "logs", "node-producer", cwd=work, check=False
        ).stdout

    def twin():
        return sh(
            "docker", "compose", "-p", "nodesmoke", "logs", "node-twin", cwd=work, check=False
        ).stdout

    wait_for(lambda: "pushed " in logs(), "the producer")
    wait_for(lambda: "(5 reads)" in twin(), "the twin to read the realtime point")

    def down():
        sh("docker", "compose", "-p", "nodesmoke", "down", "-v", cwd=work, check=False)

    return "http://127.0.0.1:8787", down, logs


def k3d(work: Path, prod: str):
    sh(
        "k3d",
        "cluster",
        "create",
        "nodesmoke",
        "--wait",
        "--no-lb",
        "--k3s-arg",
        "--disable=traefik@server:0",
    )
    sh("k3d", "image", "import", "axiom-runtime:dev", "-c", "nodesmoke")
    sh("kubectl", "create", "configmap", "smoke-edge-config", f"--from-file={work / 'edge'}")
    sh(
        "kubectl",
        "create",
        "configmap",
        "smoke-producer-config",
        f"--from-file={HERE / 'push.py'}",
        f"--from-file={HERE / 'twin.py'}",
    )
    sh(
        "kubectl",
        "create",
        "secret",
        "generic",
        "smoke-producer-secret",
        f"--from-literal=PRODUCER_TOKEN={prod}",
    )
    sh("helm", "install", "node", str(CHART), "-f", str(HERE / "values-smoke.yaml"))

    def logs():
        return sh("kubectl", "logs", "statefulset/node-producer", check=False).stdout

    wait_for(lambda: "pushed " in logs(), "the producer", timeout=900)

    def twin():
        return sh("kubectl", "logs", "statefulset/node-twin", check=False).stdout

    wait_for(lambda: "(5 reads)" in twin(), "the twin to read the realtime point", timeout=600)
    pf = _reforward()

    def down():
        pf.terminate()
        sh("k3d", "cluster", "delete", "nodesmoke", check=False)

    return f"http://127.0.0.1:{FORWARD_PORT}", down, logs


def restart(target: str, work: Path) -> None:
    """Power loss, approximated: every role stops and starts again."""
    if target == "compose" and os.environ.get("SMOKE_DAEMON_RESTART") == "1":
        # The Docker daemon itself restarts (an upgrade, a crash): every
        # container stops at once and comes back only by its restart policy.
        sh("sudo", "systemctl", "restart", "docker")

        def up():
            r = sh(
                "docker",
                "compose",
                "-p",
                "nodesmoke",
                "ps",
                "--format",
                "{{.Service}} {{.State}}",
                cwd=work,
                check=False,
            )
            return r.returncode == 0 and r.stdout.count("running") >= 3

        wait_for(up, "every role after the Docker daemon restarted", timeout=600)
    elif target == "compose":
        sh("docker", "compose", "-p", "nodesmoke", "restart", cwd=work)
    else:
        sh("docker", "restart", "k3d-nodesmoke-server-0")

        # The API server and the pods come back in their own time; `kubectl wait`
        # fails at once while no pod matches, so retry until all three are ready.
        def ready():
            r = sh(
                "kubectl",
                "wait",
                "--for=condition=ready",
                "pod",
                "-l",
                "app.kubernetes.io/instance=node",
                "--timeout=20s",
                check=False,
            )
            return r.returncode == 0 and r.stdout.count("condition met") >= 3

        wait_for(ready, "the pods after a restart", timeout=900)


def main() -> int:
    target = sys.argv[1]
    work = Path(tempfile.mkdtemp(prefix=f"node-smoke-{target}-"))
    (work / "edge").mkdir()
    prod, down_tok = issue(work)
    url, teardown, logs = (compose if target == "compose" else k3d)(work, prod)
    pf = None
    try:
        pushed = int(logs().split("pushed ")[-1].split()[0])
        first, second = pull(work, url, down_tok)
        # After a restart the producer pushes the same batches again; the edge
        # must land none of them twice, and the downstream must pull nothing new.
        restart(target, work)
        pf = _reforward() if target == "k3d" else None
        # Compose keeps the earlier log lines across a restart; Kubernetes starts a fresh log.
        wait_for(
            lambda: logs().count("pushed ") >= (2 if target == "compose" else 1),
            "the producer after restart",
        )
        third = pull_again(work, down_tok)
    finally:
        if pf:
            pf.terminate()
        teardown()
    assert pushed == EXPECT, f"producer pushed {pushed}, expected {EXPECT}"
    assert first.get("rows_landed") == EXPECT, first
    assert second.get("records") == 0, second
    assert third.get("rows_landed", 0) == 0, f"duplicates after restart: {third}"
    print(
        f"{target}: producer pushed {pushed}, twin read the realtime point, downstream pulled "
        f"{first['rows_landed']}, rerun pulled {second['records']}, after a full restart pulled "
        f"{third.get('rows_landed', 0)} new rows"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
