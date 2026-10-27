"""Zero-gap upgrade for a serving role: switch it blue -> green -> blue while
requests keep arriving, and count every request that was not answered.

Roles: serve (the platform's gate, API and web app), rag, model, chat. The
load comes from where real callers are: through the role's published front on
a single host, and from inside the cluster through its Service on Kubernetes
(a port-forward sticks to one pod and would not see the switch at all).

Pass: requests were made throughout, and none was refused or failed.
Usage: zero_gap_serving.py compose|k3d ROLE [--image-tag TAG]
"""

import argparse
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import node_chain_smoke as chain  # noqa: E402

ROOT, CHART = chain.ROOT, chain.CHART
PROJECT = "zerogapserve"
BASE = ["image.repository=axiom-runtime", "image.tag=dev", "image.pullPolicy=Never", "profile=node"]

#: Each role's port and readiness path, from values.yaml.
ROLES = {
    "serve": (8770, "/healthz"),
    "rag": (8790, "/healthz"),
    "model": (8080, "/health"),
    "chat": (8080, "/health"),
}
OWN_IMAGE = {"model", "chat"}
DB_IMAGE = "pgvector/pgvector:pg16"

PROBE = """
import json, os, sys, time, urllib.request
url, until = sys.argv[1], time.time() + float(sys.argv[2])
ok = failed = 0
errors = []
while time.time() < until:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            ok += 1 if r.status < 400 else 0
            failed += 0 if r.status < 400 else 1
    except Exception as e:  # noqa: BLE001 - every kind of failure counts
        failed += 1
        errors.append(f"{type(e).__name__}: {e}"[:160])
    time.sleep(0.05)
print("summary " + json.dumps({"ok": ok, "failed": failed, "errors": errors[:10]}), flush=True)
"""


def green(role: str, tag: str | None) -> list[str]:
    """The release that adds a green slot: new packages, or a new image tag."""
    if role in OWN_IMAGE:
        return (
            [f"roles.{role}.blueGreen.slots.green.image.tag={tag}"]
            if tag
            else [f"roles.{role}.blueGreen.slots.green.image.pullPolicy=IfNotPresent"]
        )
    return [f"roles.{role}.blueGreen.slots.green.packages.version=0.70.6-green"]


def bluegreen(*a):
    r = chain.sh(
        sys.executable,
        "-m",
        "axiom.infra.deploy.bluegreen",
        *a,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
    )
    if r.returncode:
        raise SystemExit(f"switch failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout


#: Roles that need the platform's database (ADR-174) get a throwaway one.
NEEDS_DB = {"serve", "rag"}
DB_PASSWORD = secrets.token_hex(16)  # this throwaway stack's only; never a real one


def db_env() -> str:
    dsn = f"postgresql://axiom:{DB_PASSWORD}@node-medallion:5432/axiom_db"
    return f"AXIOM_DB_URL={dsn}\nAXIOM_RAG_DSN={dsn}\n"


def role_sets(role: str, extra: list[str]) -> list[str]:
    sets = [*BASE, f"roles.{role}.enabled=true", *extra]
    if role in NEEDS_DB:
        sets += [
            "roles.medallion.enabled=true",
            f"roles.medallion.image={DB_IMAGE}",
            # The chart's database asks for 50Gi and a full CPU. On this k3d
            # node that PVC stays unbound and the pod never schedules, so the
            # role waits out AXIOM_WAIT_FOR and exits. The proof needs a small
            # database beside the role, not that request.
            "roles.medallion.storage=1Gi",
            "roles.medallion.resources.cpu=100m",
            "roles.medallion.resources.memory=512Mi",
            "roles.medallion.secretFrom=db",
            f"roles.{role}.secretFrom=db",
            f"roles.{role}.env.AXIOM_WAIT_FOR=node-medallion:5432",
        ]
    if role == "chat":
        # A UI from its own image runs as that image's user. A full CPU or
        # 2Gi request leaves the green slot Pending; no limit lets the UI
        # take the node and the prober never starts. 100m/1Gi schedules both
        # slots and leaves room for the prober. The chart's embedding engine
        # is external, so this limit holds: the local model load is what
        # OOM-killed the slot here.
        sets.append("roles.chat.runAsUser=0")
        sets.append("roles.chat.resources.cpu=100m")
        sets.append("roles.chat.resources.memory=1Gi")
    else:
        # A slot asks for a full CPU. Once the database is also scheduled,
        # the green slot stays Pending (`Node: <none>`) until the rollout
        # times out. The proof only needs the health path.
        sets.append(f"roles.{role}.resources=none")
    return sets


def compose(role: str, tag: str | None, duration: float) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"zerogap-{role}-"))
    port, path = ROLES[role]
    host_port = chain.free_port()

    def generate(*extra):
        args = []
        for s in role_sets(role, [f"roles.{role}.publish=127.0.0.1:{host_port}", *extra]):
            args += ["--set", s]
        chain.sh(
            sys.executable,
            "-m",
            "axiom.infra.deploy.compose_from_chart",
            "--chart",
            str(CHART),
            *args,
            "-o",
            str(work / "compose.yaml"),
            cwd=work,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )

    dc = ["docker", "compose", "-p", PROJECT]
    (work / "secrets").mkdir()
    (work / "secrets" / "medallion.env").write_text(
        f"POSTGRES_PASSWORD={DB_PASSWORD}\nPOSTGRES_USER=axiom\nPOSTGRES_DB=axiom_db\n"
    )
    (work / "secrets" / f"{role}.env").write_text(db_env())
    chain.sh("chmod", "-R", "a+rX", str(work / "secrets"))
    generate()
    try:
        r = chain.sh(*dc, "up", "-d", cwd=work, check=False)
        if r.returncode:
            raise SystemExit(f"compose up failed:\n{r.stderr[-3000:]}")
        url = f"http://127.0.0.1:{host_port}{path}"

        def answers():
            try:
                return urllib.request.urlopen(url, timeout=3).status < 400
            except OSError:
                return False

        chain.wait_for(answers, f"the {role} front", timeout=1500)
        out: dict = {}
        load = threading.Thread(target=lambda: out.update(_probe_local(url, duration)), daemon=True)
        load.start()
        time.sleep(5)
        generate(*green(role, tag))
        common = ["--role", role, "--compose-dir", str(work), "--project", PROJECT]
        print(bluegreen("compose", *common, "--to", "green"))
        time.sleep(10)
        print(bluegreen("compose", *common, "--to", "blue"))
        load.join()
        return out
    finally:
        chain.sh(*dc, "down", "-v", cwd=work, check=False)
        chain.shutil.rmtree(work / "secrets", ignore_errors=True)


def _probe_local(url: str, duration: float) -> dict:
    r = chain.sh(sys.executable, "-c", PROBE, url, str(duration), check=False)
    return json.loads(r.stdout.rsplit("summary ", 1)[1])


def _rollout(name: str, timeout: str) -> None:
    r = chain.sh("kubectl", "rollout", "status", name, f"--timeout={timeout}", check=False)
    if r.returncode == 0:
        return
    pods = chain.sh("kubectl", "get", "pods", "-o", "wide", check=False)
    desc = chain.sh("kubectl", "describe", "pods", check=False)
    logs = chain.sh(
        "kubectl", "logs", "-l", "app.kubernetes.io/name=axiom-node",
        "--all-containers", "--tail=40", "--prefix", check=False,
    )
    raise SystemExit(
        f"{name} did not become ready\n{r.stderr}\n{pods.stdout}\n{desc.stdout[-12000:]}\n{logs.stdout[-6000:]}"
    )


def _import(image: str) -> None:
    """Put an image in k3s's containerd. The proof previously imported only
    axiom-runtime:dev, so Postgres and a role's own image had to be pulled
    from inside the cluster."""
    chain.sh("docker", "pull", image)
    chain.sh("k3d", "image", "import", image, "-c", PROJECT)


def k3d(role: str, tag: str | None, duration: float) -> dict:
    port, path = ROLES[role]
    chain.sh(
        "k3d",
        "cluster",
        "create",
        PROJECT,
        "--wait",
        "--no-lb",
        "--k3s-arg",
        "--disable=traefik@server:0",
    )
    try:
        chain.sh("k3d", "image", "import", "axiom-runtime:dev", "-c", PROJECT)
        if role in NEEDS_DB:
            _import(DB_IMAGE)
        # A role's own image (chat, model) is pulled by k3s. Importing it here
        # as well copies it onto the runner first, and that copy is what filled
        # the disk. k3s already pulled chat successfully on the previous run.
        if role in NEEDS_DB:
            chain.sh(
                "kubectl",
                "create",
                "secret",
                "generic",
                "db",
                f"--from-literal=POSTGRES_PASSWORD={DB_PASSWORD}",
                "--from-literal=POSTGRES_USER=axiom",
                "--from-literal=POSTGRES_DB=axiom_db",
                *[f"--from-literal={kv}" for kv in db_env().split()],
            )
        sets = role_sets(role, [])
        args = []
        for s in sets:
            args += ["--set", s]
        chain.sh("helm", "install", "node", str(CHART), *args)
        if role in NEEDS_DB:
            # pgvector's image creates the extension from its init script on
            # Compose. On k3s that script does not, and the schema then dies
            # with `type "vector" does not exist`. The proof's database user
            # is the superuser of this throwaway database.
            _rollout("statefulset/node-medallion", "300s")
            chain.sh(
                "kubectl", "exec", "statefulset/node-medallion", "--",
                "psql", "-U", "axiom", "-d", "axiom_db", "-c",
                "CREATE EXTENSION IF NOT EXISTS vector;",
            )
        _rollout(f"statefulset/node-{role}-blue", "1500s")
        # A role with its own image never touches axiom-runtime:dev, so the
        # import at cluster start can be gone by the time the prober asks for
        # it (ErrImageNeverPull). Import again immediately before that pod.
        chain.sh("k3d", "image", "import", "axiom-runtime:dev", "-c", PROJECT)
        # The caller lives in the cluster and reaches the role by its Service.
        chain.sh(
            "kubectl",
            "run",
            "prober",
            "--image=axiom-runtime:dev",
            "--image-pull-policy=Never",
            "--restart=Never",
            "--command",
            "--",
            "python",
            "-c",
            PROBE,
            f"http://node-{role}:{port}{path}",
            str(duration),
        )
        try:
            chain.wait_for(
                lambda: "Running" in chain.sh("kubectl", "get", "pod", "prober", check=False).stdout,
                "the prober",
                timeout=300,
            )
        except SystemExit:
            pods = chain.sh("kubectl", "get", "pods", "-o", "wide", check=False)
            desc = chain.sh("kubectl", "describe", "pod", "prober", check=False)
            raise SystemExit(f"the prober did not start\n{pods.stdout}\n{desc.stdout[-4000:]}")
        time.sleep(5)
        helm = []
        for s in [*sets, *green(role, tag)]:
            helm += ["--helm-arg", f"--set {s}"]
        print(bluegreen("k8s", "--role", role, "--to", "green", "--chart", str(CHART), *helm))
        time.sleep(10)
        print(bluegreen("k8s", "--role", role, "--to", "blue", "--chart", str(CHART), *helm))

        def finished():
            return "summary " in chain.sh("kubectl", "logs", "prober", check=False).stdout

        chain.wait_for(finished, "the prober to finish", timeout=int(duration) + 600)
        logs = chain.sh("kubectl", "logs", "prober").stdout
        return json.loads(logs.rsplit("summary ", 1)[1])
    finally:
        chain.sh("k3d", "cluster", "delete", PROJECT, check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", choices=["compose", "k3d"])
    ap.add_argument("role", choices=sorted(ROLES))
    ap.add_argument("--image-tag", help="the green slot's image tag, for a role with its own image")
    ap.add_argument("--duration", type=float, default=180)
    args = ap.parse_args()
    out = (compose if args.target == "compose" else k3d)(args.role, args.image_tag, args.duration)
    print(
        f"{args.target} {args.role}: answered {out['ok']}, failed {out['failed']} {out['errors']}"
    )
    assert out["ok"] > 100, f"too few requests to prove anything: {out}"
    assert out["failed"] == 0, f"{out['failed']} requests failed during the deploys"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
