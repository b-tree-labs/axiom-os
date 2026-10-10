"""Prove the archive role (values-archive.yaml) end to end on one host, and
measure it at a profile's limits.

Brings up the edge, its TimescaleDB medallion, the schema init and the conform
companion from the chart on Compose, pushes READINGS readings through the
edge, runs one conform pass, and checks through the read-only role that silver
holds every reading exactly once. Samples each container's memory throughout,
so the profile's limits are checked against what the roles actually use.

The normalizer is a small package (smoke_normalizer) installed beside the
pinned Axiom, the way a site installs its own.

Usage: archive_proof.py [--profile lowpower] [--readings 200000]
Needs helm and docker. Prints one JSON summary line; exits non-zero on a gap.
"""

import argparse
import datetime as dt
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import node_chain_smoke as chain  # noqa: E402

PROJECT = "archiveproof"
CHART_PIN = __import__("yaml").safe_load((chain.CHART / "values.yaml").read_text())["packages"][
    "pin"
]
ROWS_PER_BATCH = 2000


def build_normalizer(work: Path) -> str:
    out = work / "config" / "edge"
    chain.sh(
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "-q",
        "--no-deps",
        "-w",
        str(out),
        str(HERE / "smoke_normalizer"),
    )
    return next(out.glob("smoke_normalizer-*.whl")).name


def generate(work: Path, profile: str, wheel: str, port: int, pin: str) -> None:
    chain.sh(
        sys.executable,
        "-m",
        "axiom.infra.deploy.compose_from_chart",
        "--chart",
        str(chain.CHART),
        "-f",
        str(chain.CHART / f"values-{profile}.yaml"),
        "-f",
        str(chain.CHART / "values-archive.yaml"),
        "--set",
        "image.repository=axiom-runtime",
        "--set",
        "image.tag=dev",
        "--set",
        "image.pullPolicy=Never",
        "--set",
        f"roles.edge.publish=127.0.0.1:{port}",
        "--set",
        f"packages.pin={pin} /config/{wheel}",
        "-o",
        str(work / "compose.yaml"),
        cwd=work,
        env={**os.environ, "PYTHONPATH": str(chain.ROOT / "src")},
    )


def push(url: str, tok: str, readings: int) -> int:
    base = dt.datetime(2026, 10, 8, tzinfo=dt.UTC)
    landed, n = 0, 0
    for b in range((readings + ROWS_PER_BATCH - 1) // ROWS_PER_BATCH):
        rows = []
        for _ in range(min(ROWS_PER_BATCH, readings - n)):
            ts = base + dt.timedelta(milliseconds=100 * (n // 10))
            rows.append(
                {
                    "channel": f"C{n % 10}",
                    "ts": ts.isoformat().replace("+00:00", "Z"),
                    "value": n,
                    "unit": "1",
                }
            )
            n += 1
        body = {
            "source": "smoke-src",
            "batches": [{"item_id": f"archive-{b}", "schema_ref": "smoke/rows-v1", "rows": rows}],
        }
        req = urllib.request.Request(
            url + "/ingest/rows",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            landed += json.loads(r.read())["rows_landed"]
    return landed


class MemorySampler(threading.Thread):
    """Peak memory per container of the project, from `docker stats`."""

    def __init__(self):
        super().__init__(daemon=True)
        self.peak: dict[str, float] = {}
        self.limit: dict[str, float] = {}
        self.stop = threading.Event()

    @staticmethod
    def _mib(s: str) -> float:
        s = s.strip()
        for suffix, f in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024), ("B", 1 / 1024 / 1024)):
            if s.endswith(suffix):
                return float(s[: -len(suffix)]) * f
        return 0.0

    def run(self):
        while not self.stop.is_set():
            out = chain.sh(
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}}\t{{.MemUsage}}",
                check=False,
            ).stdout
            for line in out.splitlines():
                name, _, usage = line.partition("\t")
                if not name.startswith(PROJECT + "-"):
                    continue
                used, _, lim = usage.partition("/")
                svc = name[len(PROJECT) + 1 :].rsplit("-", 1)[0]
                self.peak[svc] = max(self.peak.get(svc, 0.0), self._mib(used))
                self.limit[svc] = self._mib(lim)
            time.sleep(2)


#: What may run in the archive role, and nothing else: the edge, its front,
#: the conform loop, the database, and their shells and sleeps.
EXPECTED_PORTS = {"node-edge": {8787}, "node-edge-blue": {8787}, "node-medallion": {5432}}
AGENTISH = ("chat", "agent", "llm", "ollama", "llama", "openai", "anthropic", "webui", "langfuse")

_LISTENING = (
    "import glob;"
    "print(sorted({int(l.split()[1].split(':')[1],16) for f in glob.glob('/proc/net/tcp*')"
    " for l in open(f).readlines()[1:] if l.split()[3]=='0A'"
    # Docker's embedded DNS resolver listens on 127.0.0.11 in every container.
    " and l.split()[1].split(':')[0] not in ('0B00007F','0000000000000000FFFF00000B00007F')}))"
)
_REFUSED = (
    "from axiom.llm.gateway import LLMRefusedByPolicy, _post_with_rate_limit_retry as post\n"
    "try:\n"
    "    post(None, 'http://127.0.0.1:9/v1/chat/completions', {}, {})\n"
    "    print('CALLED')\n"
    "except LLMRefusedByPolicy as e:\n"
    "    print('REFUSED', e)\n"
)


def no_agents_no_model(work: Path) -> dict:
    """C-48: the archive role starts no chat and no agent, and nothing in it can
    call a language model: the gateway refuses under the declared policy.
    Evidence: each container's processes and listening ports, and the refusal."""
    names = chain.sh(
        "docker", "compose", "-p", PROJECT, "ps", "--format", "{{.Service}}", cwd=work
    ).stdout.split()
    procs, ports, problems = {}, {}, []
    for svc in names:
        cid = chain.sh("docker", "compose", "-p", PROJECT, "ps", "-q", svc, cwd=work).stdout.strip()
        lines = chain.sh("docker", "top", cid, check=False).stdout.splitlines()
        col = lines[0].index("CMD") if lines and "CMD" in lines[0] else 0
        top = [ln[col:].strip() for ln in lines[1:]]
        procs[svc] = top
        for line in top:
            if any(w in line.lower() for w in AGENTISH):
                problems.append(f"{svc} runs {line!r}")
        py = "python3" if svc == "node-medallion" else "python"
        r = chain.sh("docker", "exec", cid, py, "-c", _LISTENING, check=False)
        if r.returncode:  # no Python in the image (nginx): read the table with awk
            r = chain.sh(
                "docker",
                "exec",
                cid,
                "sh",
                "-c",
                'cat /proc/net/tcp /proc/net/tcp6 | awk \'NR>1 && $4=="0A" {split($2,a,":"); if (a[1]!="0B00007F") print a[2]}\'',
                check=False,
            )
            found = sorted({int(h, 16) for h in r.stdout.split()})
        else:
            found = json.loads(r.stdout.strip() or "[]")
        ports[svc] = found
        extra = set(found) - EXPECTED_PORTS.get(svc, set())
        if extra:
            problems.append(f"{svc} listens on {sorted(extra)}")
    refusals = {}
    for svc in ("node-edge-blue", "node-edge-blue-conform"):
        r = chain.sh(
            "docker",
            "compose",
            "-p",
            PROJECT,
            "exec",
            "-T",
            svc,
            "/usr/local/bin/axiom-entrypoint",
            "python",
            "-c",
            _REFUSED,
            cwd=work,
            check=False,
        )
        refusals[svc] = (r.stdout.strip() or r.stderr.strip())[-300:]
        if not refusals[svc].startswith("REFUSED"):
            problems.append(f"{svc}: a model call was not refused: {refusals[svc]}")
    return {
        "ok": not problems,
        "problems": problems,
        "processes": procs,
        "ports": ports,
        "llm": refusals,
    }


def psql(work: Path, sql: str, user: str, password: str) -> str:
    return chain.sh(
        "docker",
        "compose",
        "-p",
        PROJECT,
        "exec",
        "-T",
        "-e",
        f"PGPASSWORD={password}",
        "node-medallion",
        "psql",
        "-h",
        "127.0.0.1",
        "-U",
        user,
        "-d",
        "axiom_db",
        "-tAc",
        sql,
        cwd=work,
    ).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="lowpower")
    ap.add_argument("--readings", type=int, default=200_000)
    ap.add_argument("--keep", action="store_true", help="leave the stack up afterwards")
    ap.add_argument(
        "--from-source",
        action="store_true",
        help="run this checkout's Axiom (built as a wheel) instead of the pinned release",
    )
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="archive-proof-"))
    (work / "edge").mkdir()
    prod, _down = chain.issue(work)
    shutil.copytree(work / "edge", work / "config" / "edge")
    wheel = build_normalizer(work)
    pin = CHART_PIN
    if args.from_source:
        out = work / "config" / "edge"
        chain.sh(
            sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", str(out), str(chain.ROOT)
        )
        pin = "/config/" + next(out.glob("axiom_os_lm-*.whl")).name
    # Test credentials for this throwaway stack only, generated here and
    # removed with it; a real install writes these from the vault.
    pg, reader = secrets.token_hex(16), secrets.token_hex(16)
    (work / "secrets").mkdir()
    (work / "secrets" / "medallion.env").write_text(
        f"POSTGRES_PASSWORD={pg}\nPOSTGRES_USER=axiom\nPOSTGRES_DB=axiom_db\n"
    )
    (work / "secrets" / "edge.env").write_text(
        f"AXIOM_DB_URL=postgresql://axiom:{pg}@node-medallion:5432/axiom_db\nAXIOM_READER_PASSWORD={reader}\n"
    )
    for p in (work / "config", work / "secrets"):
        chain.sh("chmod", "-R", "a+rX", str(p))
    port = chain.free_port()
    generate(work, args.profile, wheel, port, pin)
    url = f"http://127.0.0.1:{port}"

    sampler = MemorySampler()
    sampler.start()
    t0 = time.time()
    try:
        r = chain.sh("docker", "compose", "-p", PROJECT, "up", "-d", cwd=work, check=False)
        if r.returncode:
            raise SystemExit(f"compose up failed:\n{r.stderr[-3000:]}")

        def ready():
            try:
                return urllib.request.urlopen(url + "/healthz", timeout=3).status == 200
            except OSError:
                return False

        chain.wait_for(ready, "the archive edge", timeout=1500)
        up_s = time.time() - t0
        t1 = time.time()
        landed = push(url, prod, args.readings)
        push_s = time.time() - t1
        conform_svc = next(
            s
            for s in chain.sh(
                "docker", "compose", "-p", PROJECT, "ps", "--services", cwd=work
            ).stdout.split()
            if s.endswith("conform")
        )
        t2 = time.time()
        run = chain.sh(
            "docker",
            "compose",
            "-p",
            PROJECT,
            "exec",
            "-T",
            conform_svc,
            "/usr/local/bin/axiom-entrypoint",  # exec skips the entrypoint, which puts the role's packages on PATH
            "sh",
            "-c",
            'export AXI_STATE_DIR=/data/state; for d in /data/bronze/*/; do axi data conform-run --bronze-root "$d" || exit 1; done',
            cwd=work,
            check=False,
        )
        conform_s = time.time() - t2
        if run.returncode:
            raise SystemExit(f"conform failed:\n{run.stdout[-2000:]}\n{run.stderr[-2000:]}")
        total = int(psql(work, "select count(*) from silver.signals", "archive_reader", reader))
        distinct = int(
            psql(
                work,
                "select count(distinct (channel, ts)) from silver.signals",
                "archive_reader",
                reader,
            )
        )
        refused_write = (
            chain.sh(
                "docker",
                "compose",
                "-p",
                PROJECT,
                "exec",
                "-T",
                "-e",
                f"PGPASSWORD={reader}",
                "node-medallion",
                "psql",
                "-h",
                "127.0.0.1",
                "-U",
                "archive_reader",
                "-d",
                "axiom_db",
                "-tAc",
                "delete from silver.signals",
                cwd=work,
                check=False,
            ).returncode
            != 0
        )
        quiet = no_agents_no_model(work)
        health = json.loads(urllib.request.urlopen(url + "/healthz", timeout=10).read())
        watched = {d["label"]: d for d in health.get("disks") or []}
        # The same watch with the alarm floor raised above the free space: the
        # alarm the node would raise as the database volume fills.
        alarm = chain.sh(
            "docker",
            "compose",
            "-p",
            PROJECT,
            "exec",
            "-T",
            "-e",
            "AXIOM_DISK_ALARM_FREE_PERCENT=100",
            "node-edge-blue",
            "/usr/local/bin/axiom-entrypoint",
            "python",
            "-c",
            "from axiom.extensions.builtins.data_platform.ingest_sink.headroom import watched;"
            "print([d['label'] for d in watched() if d['alarm']])",
            cwd=work,
            check=False,
        ).stdout.strip()
        time.sleep(4)
    finally:
        sampler.stop.set()
        sampler.join(timeout=10)
        if not args.keep:
            chain.sh("docker", "compose", "-p", PROJECT, "down", "-v", cwd=work, check=False)
        shutil.rmtree(work / "secrets", ignore_errors=True)

    summary = {
        "profile": args.profile,
        "package": pin,
        "pushed": args.readings,
        "landed": landed,
        "silver": total,
        "silver_distinct": distinct,
        "reader_cannot_write": refused_write,
        "database_disk_watched": "database" in watched and "total_bytes" in watched["database"],
        "database_disk_alarm_raised": alarm == "['database']",
        "no_agents_no_model": quiet,
        "seconds": {"up": round(up_s), "push": round(push_s), "conform": round(conform_s)},
        "conform_readings_per_s": round(total / conform_s) if conform_s else None,
        "peak_mib": {k: round(v) for k, v in sorted(sampler.peak.items())},
        "limit_mib": {k: round(v) for k, v in sorted(sampler.limit.items())},
    }
    print("summary " + json.dumps(summary))
    ok = (
        landed == total == distinct == args.readings
        and refused_write
        and summary["database_disk_watched"]
        and summary["database_disk_alarm_raised"]
        and quiet["ok"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
