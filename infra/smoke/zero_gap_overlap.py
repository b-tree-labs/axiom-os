"""Zero-gap update of a collector by overlap, from the chart.

The producer role runs under the switch supervisor (overlap.enabled). Two
switches (SIGHUP) start a new copy beside the old one each time, inside the
running container. Pass: no push was refused, every 0.2 s reading between
the first and the last was sent by some copy (no gap), the downstream pulled
each reading exactly once (no duplicate), each copy kept its own journal, and
the registry is empty once every copy has left.

Usage: zero_gap_overlap.py compose|k3d
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
SOURCE: list[str] = []
SETS = [
    "roles.twin.enabled=false",
    "roles.producer.command={python,/config/overlap_collector.py}",
    "roles.producer.overlap.enabled=true",
    "roles.producer.env.JOURNAL=/data/journal",
]


def source_image() -> list[str]:
    """The runtime image with this checkout's Axiom as a wheel inside it, and
    the producer pinned to it: the supervisor's per-copy instance id is newer
    than any release. Returns the chart settings that use it."""
    ctx = Path(tempfile.mkdtemp(prefix="axiom-src-"))
    chain.sh(sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", str(ctx), str(ROOT))
    wheel = next(ctx.glob("axiom_os_lm-*.whl")).name
    (ctx / "Dockerfile").write_text(f"FROM axiom-runtime:dev\nCOPY {wheel} /opt/axiom/{wheel}\n")
    chain.sh("docker", "build", "-q", "-t", "axiom-runtime:src", str(ctx))
    return [
        "image.tag=src",
        f"roles.producer.packages.pin=/opt/axiom/{wheel}",
        "roles.producer.packages.version=src",
    ]


def switch_and_settle(started, registered, switch, timeout: int = 900) -> None:
    """Start reading, then switch twice, each time waiting until the new copy
    is running and the old one has left (one registered instance again)."""
    chain.wait_for(
        lambda: started() >= 1 and registered() == "1", "the first copy", timeout=timeout
    )
    time.sleep(10)
    for n in (2, 3):
        switch()
        chain.wait_for(lambda n=n: started() >= n and registered() == "1", f"copy {n} to take over")
        time.sleep(10)


def overlap(target: str, **kw) -> None:
    args = [target, "--role", "producer", "--overlap"]
    for k, v in kw.items():
        args += [f"--{k.replace('_', '-')}", str(v)]
    r = chain.sh(
        sys.executable,
        "-m",
        "axiom.infra.deploy.bluegreen",
        *args,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
    )
    if r.returncode:
        raise SystemExit(f"overlap switch failed:\n{r.stdout}\n{r.stderr}")


READ = (
    "import glob,json,os;"
    "print(json.dumps({'journals':{os.path.basename(d):open(d+'/ticks').read().split()"
    " for d in glob.glob('/data/journal@*')},"
    "'refused':sum(int(open(f).read()) for f in glob.glob('/data/journal@*/refused')),"
    "'refusals':[ln for f in glob.glob('/data/journal@*/refusals') for ln in open(f) if ln.strip()],"
    "'registered':os.listdir('/data/journal.instances')}))"
)


def compose(work: Path, prod: str):
    (work / "config" / "producer").mkdir(parents=True)
    chain.shutil.copy(HERE / "overlap_collector.py", work / "config" / "producer")
    chain.shutil.copytree(work / "edge", work / "config" / "edge")
    (work / "secrets").mkdir()
    (work / "secrets" / "producer.env").write_text(f"PRODUCER_TOKEN={prod}\n")
    for p in (work / "config", work / "secrets"):
        chain.sh("chmod", "-R", "a+rX", str(p))
    args = []
    for s in [*SETS, *SOURCE]:
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
    dc = ["docker", "compose", "-p", "zerogapoverlap"]
    chain.sh(*dc, "up", "-d", cwd=work)

    def read():
        # The journals live on the role's volume; read them after the role stops.
        r = chain.sh(
            "docker",
            "run",
            "--rm",
            "-v",
            "zerogapoverlap_node-producer-data:/data",
            "--entrypoint",
            "python",
            "axiom-runtime:dev",
            "-c",
            READ,
        )
        return json.loads(r.stdout)

    common = {"compose_dir": work, "project": "zerogapoverlap"}

    def started():
        return chain.sh(*dc, "logs", "node-producer", cwd=work, check=False).stdout.count(
            " started"
        )

    def registered():
        return chain.sh(
            *dc,
            "exec",
            "-T",
            "node-producer",
            "sh",
            "-c",
            "ls /data/journal.instances | wc -l",
            cwd=work,
            check=False,
        ).stdout.strip()

    switch_and_settle(started, registered, lambda: overlap("compose", **common))
    chain.sh(
        *dc, "stop", "-t", "60", "node-producer", cwd=work
    )  # SIGTERM: the copy finishes and leaves

    def down():
        chain.sh(*dc, "down", "-v", cwd=work, check=False)

    return "http://127.0.0.1:8787", down, read


def k3d(work: Path, prod: str):
    chain.sh(
        "k3d",
        "cluster",
        "create",
        "zerogapoverlap",
        "--wait",
        "--no-lb",
        "--k3s-arg",
        "--disable=traefik@server:0",
    )
    chain.sh(
        "k3d", "image", "import", "axiom-runtime:dev", "axiom-runtime:src", "-c", "zerogapoverlap"
    )
    chain.sh("kubectl", "create", "configmap", "smoke-edge-config", f"--from-file={work / 'edge'}")
    chain.sh(
        "kubectl",
        "create",
        "configmap",
        "smoke-producer-config",
        f"--from-file={HERE / 'overlap_collector.py'}",
    )
    chain.sh(
        "kubectl",
        "create",
        "secret",
        "generic",
        "smoke-producer-secret",
        f"--from-literal=PRODUCER_TOKEN={prod}",
    )
    args = []
    for s in [*SETS, *SOURCE]:
        args += ["--set", s]
    chain.sh("helm", "install", "node", str(CHART), "-f", str(HERE / "values-smoke.yaml"), *args)
    pexec = ["kubectl", "exec", "node-producer-0", "--"]

    def started():
        return chain.sh("kubectl", "logs", "node-producer-0", check=False).stdout.count(" started")

    def registered():
        return chain.sh(
            *pexec, "sh", "-c", "ls /data/journal.instances | wc -l", check=False
        ).stdout.strip()

    switch_and_settle(started, registered, lambda: overlap("k8s"), timeout=1500)
    # Stop the role (SIGTERM: the last copy finishes and leaves), then read
    # its volume from a pod that mounts it.
    chain.sh("kubectl", "scale", "statefulset/node-producer", "--replicas=0")
    chain.sh(
        "kubectl", "wait", "--for=delete", "pod/node-producer-0", "--timeout=180s", check=False
    )
    reader = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "journal-reader"},
        "spec": {
            "restartPolicy": "Never",
            "securityContext": {"runAsUser": 10001, "fsGroup": 10001},
            "containers": [
                {
                    "name": "r",
                    "image": "axiom-runtime:dev",
                    "imagePullPolicy": "Never",
                    "command": ["python", "-c", READ],
                    "volumeMounts": [{"name": "d", "mountPath": "/data"}],
                }
            ],
            "volumes": [
                {"name": "d", "persistentVolumeClaim": {"claimName": "data-node-producer-0"}}
            ],
        },
    }
    (work / "reader.json").write_text(json.dumps(reader))
    chain.sh("kubectl", "apply", "-f", str(work / "reader.json"))

    def read():
        chain.wait_for(
            lambda: (
                "Succeeded"
                in chain.sh(
                    "kubectl",
                    "get",
                    "pod",
                    "journal-reader",
                    "-o",
                    "jsonpath={.status.phase}",
                    check=False,
                ).stdout
            ),
            "the journal reader",
            timeout=300,
        )
        return json.loads(chain.sh("kubectl", "logs", "journal-reader").stdout)

    pf = chain._reforward()

    def down():
        pf.terminate()
        chain.sh("k3d", "cluster", "delete", "zerogapoverlap", check=False)

    return f"http://127.0.0.1:{chain.FORWARD_PORT}", down, read


def main() -> int:
    target = sys.argv[1]
    global SOURCE
    SOURCE = source_image()
    work = Path(tempfile.mkdtemp(prefix=f"overlap-{target}-"))
    (work / "edge").mkdir()
    prod, down_tok = chain.issue(work)
    url, teardown, read = (compose if target == "compose" else k3d)(work, prod)
    try:
        state = read()
        first, second = chain.pull(work, url, down_tok)
    finally:
        if not os.environ.get("KEEP"):
            teardown()
    copies = state["journals"]
    ticks = sorted({int(t) for v in copies.values() for t in v})
    gaps = [b for a, b in zip(ticks, ticks[1:], strict=False) if b - a > 1]
    print(
        json.dumps(
            {
                "copies": {k: len(v) for k, v in copies.items()},
                "readings": len(ticks),
                "gaps": gaps,
                "refused": state["refused"],
                "refusals": state.get("refusals", []),
                "still_registered": state["registered"],
                "pulled": first.get("rows_landed"),
                "rerun": second.get("records"),
            }
        )
    )
    assert len(copies) >= 3, f"expected three copies (two overlaps), saw {list(copies)}"
    assert state["refused"] == 0, (
        f"{state['refused']} pushes refused: {state.get('refusals', [])}"
    )
    assert not gaps, f"readings missing after ticks {gaps[:5]}"
    assert first.get("rows_landed") == len(ticks), (
        f"pulled {first.get('rows_landed')} for {len(ticks)} readings"
    )
    assert second.get("records") == 0, f"duplicates on rerun: {second}"
    assert state["registered"] == [], f"copies still registered: {state['registered']}"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
