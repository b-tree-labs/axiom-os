"""Zero-gap upgrade: deploy the edge blue -> green and back under live load.

Pass: 0 refused requests at the edge, and the downstream receives exactly the
rows that were sent (no loss) and a rerun receives nothing (no duplicates).
Usage: zero_gap_upgrade.py compose|k3d
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import node_chain_smoke as chain  # noqa: E402

ROOT, CHART = chain.ROOT, chain.CHART
LIVE = [
    "roles.producer.env.MODE=continuous",
    "roles.producer.env.DURATION=150",
    "roles.producer.env.INTERVAL=0.2",
    "roles.producer.env.ROWS=10",
    "roles.twin.enabled=false",
]
GREEN = ["roles.edge.blueGreen.slots.green.packages.version=0.68.0-green"]


def bluegreen(*a, cwd=None):
    r = chain.sh(
        sys.executable,
        "-m",
        "axiom.infra.deploy.bluegreen",
        *a,
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
    )
    if r.returncode:
        raise SystemExit(f"switch failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout


def summary(logs: str) -> dict:
    return json.loads(logs.rsplit("summary ", 1)[1].splitlines()[0])


def compose(work: Path, prod: str):
    (work / "config" / "producer").mkdir(parents=True)
    chain.shutil.copy(HERE / "push.py", work / "config" / "producer" / "push.py")
    chain.shutil.copytree(work / "edge", work / "config" / "edge")
    (work / "secrets").mkdir()
    (work / "secrets" / "producer.env").write_text(f"PRODUCER_TOKEN={prod}\n")
    for p in (work / "config", work / "secrets"):
        chain.sh("chmod", "-R", "a+rX", str(p))

    def generate(*extra):
        args = []
        for s in [*LIVE, *extra]:
            args += ["--set", s]
        chain.sh(
            sys.executable,
            "-m",
            "axiom.infra.deploy.compose_from_chart",
            "--chart",
            str(CHART),
            "-f",
            str(HERE / "values-smoke.yaml"),
            *args,
            "-o",
            str(work / "compose.yaml"),
            cwd=work,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )

    generate()
    dc = ["docker", "compose", "-p", "zerogap"]
    chain.sh(*dc, "up", "-d", cwd=work)

    def logs():
        return chain.sh(*dc, "logs", "node-producer", cwd=work, check=False).stdout

    chain.wait_for(lambda: "progress" in logs(), "the producer to start pushing")
    generate(*GREEN)  # the release that adds the green slot
    print(
        bluegreen(
            "compose",
            "--role",
            "edge",
            "--to",
            "green",
            "--compose-dir",
            str(work),
            "--project",
            "zerogap",
        )
    )
    time.sleep(20)
    print(
        bluegreen(
            "compose",
            "--role",
            "edge",
            "--to",
            "blue",
            "--compose-dir",
            str(work),
            "--project",
            "zerogap",
        )
    )
    chain.wait_for(lambda: "summary " in logs(), "the producer to finish", timeout=600)

    def down():
        chain.sh(*dc, "down", "-v", cwd=work, check=False)

    return "http://127.0.0.1:8787", down, logs


def k3d(work: Path, prod: str):
    chain.sh(
        "k3d",
        "cluster",
        "create",
        "zerogap",
        "--wait",
        "--no-lb",
        "--k3s-arg",
        "--disable=traefik@server:0",
    )
    chain.sh("k3d", "image", "import", "axiom-runtime:dev", "-c", "zerogap")
    chain.sh("kubectl", "create", "configmap", "smoke-edge-config", f"--from-file={work / 'edge'}")
    chain.sh(
        "kubectl",
        "create",
        "configmap",
        "smoke-producer-config",
        f"--from-file={HERE / 'push.py'}",
        f"--from-file={HERE / 'twin.py'}",
    )
    chain.sh(
        "kubectl",
        "create",
        "secret",
        "generic",
        "smoke-producer-secret",
        f"--from-literal=PRODUCER_TOKEN={prod}",
    )
    live = []
    for s in LIVE:
        live += ["--set", s]
    chain.sh("helm", "install", "node", str(CHART), "-f", str(HERE / "values-smoke.yaml"), *live)

    def logs():
        return chain.sh("kubectl", "logs", "statefulset/node-producer", check=False).stdout

    chain.wait_for(lambda: "progress" in logs(), "the producer to start pushing", timeout=900)
    helm = ["--helm-arg", f"-f {HERE / 'values-smoke.yaml'}"]
    for s in [*LIVE, *GREEN]:
        helm += ["--helm-arg", f"--set {s}"]
    print(bluegreen("k8s", "--role", "edge", "--to", "green", "--chart", str(CHART), *helm))
    time.sleep(20)
    print(bluegreen("k8s", "--role", "edge", "--to", "blue", "--chart", str(CHART), *helm))
    chain.wait_for(lambda: "summary " in logs(), "the producer to finish", timeout=900)
    pf = chain._reforward()

    def down():
        pf.terminate()
        chain.sh("k3d", "cluster", "delete", "zerogap", check=False)

    return f"http://127.0.0.1:{chain.FORWARD_PORT}", down, logs


def main() -> int:
    target = sys.argv[1]
    work = Path(tempfile.mkdtemp(prefix=f"zerogap-{target}-"))
    (work / "edge").mkdir()
    prod, down_tok = chain.issue(work)
    url, teardown, logs = (compose if target == "compose" else k3d)(work, prod)
    try:
        s = summary(logs())
        first, second = chain.pull(work, url, down_tok)
    finally:
        teardown()
    print(
        f"{target}: sent {s['sent']} rows in {s['batches']} batches, refused {s['refused']}; "
        f"downstream pulled {first.get('rows_landed')}, rerun {second.get('records')}"
    )
    assert s["refused"] == 0, f"{s['refused']} requests refused during the deploys"
    assert first.get("rows_landed") == s["sent"], f"missing rows: sent {s['sent']}, pulled {first}"
    assert second.get("records") == 0, f"duplicates: {second}"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
