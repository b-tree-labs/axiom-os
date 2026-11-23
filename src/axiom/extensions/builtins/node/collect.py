# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Read the machine: processes, units, ports, GPU, disks, served models.

Two layers on purpose. The parsers are pure functions over the text the
standard tools print (``ps``, ``systemctl``, ``ss``, ``nvidia-smi``, ``df``)
and over the JSON a model server answers, so a test can hand them a
transcript from a real machine and ask what KEEL would have said. The
:class:`Collectors` bundle holds the callables that produce that text on a
live machine; a skill takes one and a test injects one.
"""

from __future__ import annotations

import json
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: The engines KEEL recognises from a command line, and how to read each one.
#: ``model`` is the positional after the serve verb when there is one.
ENGINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("vllm", re.compile(r"\bvllm\b.*\bserve\b")),
    ("sglang", re.compile(r"\bsglang\b.*\b(launch_server|serve)\b")),
    ("llama.cpp", re.compile(r"\bllama-server\b")),
    ("text-generation-inference", re.compile(r"\btext-generation-(launcher|server)\b")),
    ("ollama", re.compile(r"\bollama\b\s+serve\b")),
    ("litellm", re.compile(r"\blitellm\b")),
)

_SKIP_FS = {
    "tmpfs",
    "devtmpfs",
    "overlay",
    "squashfs",
    "efivarfs",
    "proc",
    "sysfs",
    "cgroup2",
    "nsfs",
    "devfs",
}

#: macOS volumes that share the root's container and report the same free space, so a
#: second row says nothing new and a floor set on it can never disagree.
_SKIP_MOUNT_PREFIXES = ("/System/Volumes/",)

#: df's first column header names its block size: ``1024-blocks``, ``1K-blocks``,
#: ``512-blocks``, ``1B-blocks``.
_BLOCK_HEADER = re.compile(r"^(\d+)([KMG]?)B?-blocks$", re.IGNORECASE)
_BLOCK_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}


# -- parsers -------------------------------------------------------------------


def parse_ps(text: str) -> list[dict[str, Any]]:
    """``ps -eo pid=,user=,etimes=,args=`` → one dict per process."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        out.append(
            {
                "pid": int(parts[0]),
                "user": parts[1],
                "etimes": int(parts[2]) if parts[2].isdigit() else 0,
                "args": parts[3],
            }
        )
    return out


def _flag(args: str, name: str) -> str | None:
    m = re.search(rf"(?:^|\s){re.escape(name)}(?:=|\s+)(\S+)", args)
    return m.group(1) if m else None


def _flag_list(args: str, name: str) -> list[str]:
    """Values after a repeatable flag up to the next ``--``: ``--served-model-name a b c``."""
    m = re.search(rf"(?:^|\s){re.escape(name)}\s+((?:(?!--)\S+\s*)+)", args)
    return m.group(1).split() if m else []


def detect_engines(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Model engines among the processes, with what the command line says about each."""
    out: list[dict[str, Any]] = []
    for p in processes:
        args = p.get("args", "")
        for kind, pat in ENGINE_PATTERNS:
            if not pat.search(args):
                continue
            engine: dict[str, Any] = {
                "kind": kind,
                "pid": p["pid"],
                "user": p.get("user", ""),
                "uptime_s": p.get("etimes", 0),
                "source": "detected",
            }
            port = _flag(args, "--port")
            if port and port.isdigit():
                engine["port"] = int(port)
            elif kind == "ollama":
                # `ollama serve` takes its address from OLLAMA_HOST, default 127.0.0.1:11434.
                engine["port"], engine["host"] = 11434, "127.0.0.1"
            host = _flag(args, "--host")
            if host:
                engine["host"] = host
            if kind == "vllm":
                m = re.search(r"\bserve\s+(\S+)", args)
                if m and not m.group(1).startswith("-"):
                    engine["model"] = m.group(1)
                engine["served_as"] = _flag_list(args, "--served-model-name")
                engine["tls"] = "--ssl-certfile" in args
                engine["key_required"] = "--api-key" in args
            elif kind == "llama.cpp":
                model = _flag(args, "--model") or _flag(args, "-m")
                if model:
                    engine["model"] = model
            elif kind == "litellm":
                cfg = _flag(args, "--config")
                if cfg:
                    engine["config"] = cfg
            out.append(engine)
            break
    return out


def parse_systemctl_units(text: str) -> list[dict[str, str]]:
    """``systemctl list-units --type=service --all --no-legend --plain`` → unit rows."""
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or not parts[0].endswith(".service"):
            continue
        out.append(
            {
                "unit": parts[0],
                "load": parts[1],
                "active": parts[2],
                "sub": parts[3],
                "description": parts[4] if len(parts) > 4 else "",
            }
        )
    return out


def parse_ss_listeners(text: str) -> list[dict[str, Any]]:
    """``ss -ltnpH`` → listening sockets with the owning process when shown."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        addr, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        out.append(
            {
                "address": addr.strip("[]"),
                "port": int(port),
                "process": m.group(1) if m else "",
                "pid": int(m.group(2)) if m else None,
            }
        )
    return out


def parse_nvidia_smi_gpus(csv_text: str) -> list[dict[str, Any]]:
    """``nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader,nounits``."""
    out: list[dict[str, Any]] = []
    for line in csv_text.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        used, total = int(float(cells[2])), int(float(cells[3]))
        out.append(
            {
                "index": int(cells[0]),
                "name": cells[1],
                "memory_used_mib": used,
                "memory_total_mib": total,
                "memory_free_mib": max(total - used, 0),
            }
        )
    return out


def parse_nvidia_smi_apps(csv_text: str) -> list[dict[str, Any]]:
    """``nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits``."""
    out: list[dict[str, Any]] = []
    for line in csv_text.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        out.append(
            {
                "pid": int(cells[0]),
                "process": cells[1],
                "memory_mib": int(float(cells[2])) if cells[2].replace(".", "").isdigit() else 0,
            }
        )
    return out


def _df_block_bytes(header: str) -> int:
    """The bytes in one of df's blocks, from its header. ``-k`` is what the collector asks for."""
    for word in header.split():
        m = _BLOCK_HEADER.match(word)
        if m:
            return int(m.group(1)) * _BLOCK_UNITS[m.group(2).upper()]
    return 1024


def parse_df(text: str, readonly: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """``df -P -k`` → real filesystems with bytes (tmpfs, overlays and pseudo filesystems dropped).

    ``used_pct`` is the share of the volume that cannot be written, ``size - available``, not
    ``used / size``. On a shared APFS container a volume's own ``used`` says nothing about how
    full the disk is, and on any filesystem blocks reserved for root are not room.

    ``readonly`` mounts are dropped: a mounted image is full by construction and nothing
    can be written there, so its 100% is not an emergency. The root stays, because a sealed
    macOS root is read-only and still reports the free space of the whole container.
    """
    lines = text.splitlines()
    if not lines:
        return []
    block = _df_block_bytes(lines[0])
    out: list[dict[str, Any]] = []
    for line in lines[1:]:
        parts = line.split(None, 5)
        if len(parts) < 6 or not parts[1].isdigit():
            continue
        fs, mount = parts[0], parts[5].strip()
        size, used, avail = int(parts[1]) * block, int(parts[2]) * block, int(parts[3]) * block
        if fs in _SKIP_FS or fs.startswith(("tmpfs", "overlay", "/dev/loop")):
            continue
        if mount.startswith(_SKIP_MOUNT_PREFIXES) or (mount in readonly and mount != "/"):
            continue
        out.append(
            {
                "filesystem": fs,
                "mount": mount,
                "size_bytes": size,
                "used_bytes": used,
                "free_bytes": avail,
                "used_pct": round(100 * (size - avail) / size, 1) if size else 0.0,
            }
        )
    return out


def parse_readonly_mounts(text: str) -> set[str]:
    """Read-only mount points from ``mount``: Linux's ``(ro,...)``, macOS's ``(..., read-only)``."""
    out: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"^.+? on (/.*?)(?: type \S+)? \((.*)\)$", line)
        if m and {o.strip() for o in m.group(2).split(",")} & {"ro", "read-only"}:
            out.add(m.group(1))
    return out


def parse_vm_stat(vm_stat: str, memsize: str) -> dict[str, int]:
    """macOS memory from ``vm_stat`` and ``sysctl -n hw.memsize``, under /proc/meminfo's names.

    Available is what can be handed out without paging: free, inactive, speculative and
    purgeable pages. Empty when either tool gave nothing, because a partial reading is not one.
    """
    page = re.search(r"page size of (\d+) bytes", vm_stat)
    if not page or not memsize.strip().isdigit():
        return {}
    pages = {
        k.strip(): int(v.strip().rstrip("."))
        for k, _, v in (line.partition(":") for line in vm_stat.splitlines()[1:])
        if v.strip().rstrip(".").isdigit()
    }
    names = ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")
    return {
        "MemTotal": int(memsize.strip()),
        "MemAvailable": sum(pages.get(n, 0) for n in names) * int(page.group(1)),
    }


def parse_models_response(doc: dict[str, Any]) -> list[str]:
    """Model names from an OpenAI-style ``/v1/models`` or an Ollama ``/api/tags`` answer."""
    if "data" in doc:
        return [
            str(m.get("id")) for m in doc.get("data", []) if isinstance(m, dict) and m.get("id")
        ]
    if "models" in doc:
        return [
            str(m.get("name") or m.get("model"))
            for m in doc.get("models", [])
            if isinstance(m, dict)
        ]
    return []


# -- live collectors -----------------------------------------------------------


def run(cmd: list[str], timeout: float = 10.0) -> str:
    """The tool's stdout, or "" when it is absent or fails; KEEL reports absence, it does not crash."""
    if shutil.which(cmd[0]) is None:
        return ""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def probe_models(
    port: int, *, host: str = "127.0.0.1", tls: bool = False, timeout: float = 2.0
) -> dict[str, Any]:
    """Ask a loopback model server what it serves. Never sends a credential: a 401/403 is
    reported as "key required", which is itself a fact about the endpoint."""
    scheme = "https" if tls else "http"
    paths = ("/v1/models", "/api/tags")
    ctx = ssl.create_default_context() if tls else None
    for path in paths:
        url = f"{scheme}://{host}:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:  # noqa: S310 - loopback only
                doc = json.loads(r.read().decode() or "{}")
                return {"status": "ok", "path": path, "models": parse_models_response(doc)}
        except ssl.SSLError as exc:
            # The engine's certificate names its public host, not 127.0.0.1. KEEL never
            # turns verification off; it reports what it could not verify instead.
            return {"status": "tls not verified on loopback", "path": path, "models": [], "detail": type(exc).__name__}
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return {"status": "key required", "path": path, "models": []}
            continue
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return {"status": "unreachable", "path": "", "models": []}


@dataclass
class Collectors:
    """How KEEL reads the machine. A test replaces any of these with a lambda over a transcript."""

    ps: Callable[[], str] = lambda: run(["ps", "-eo", "pid=,user=,etimes=,args="])
    user_units: Callable[[], str] = lambda: run(
        ["systemctl", "--user", "list-units", "--type=service", "--all", "--no-legend", "--plain"]
    )
    system_units: Callable[[], str] = lambda: run(
        ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain"]
    )
    listeners: Callable[[], str] = lambda: run(["ss", "-ltnpH"])
    gpus: Callable[[], str] = lambda: run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    gpu_apps: Callable[[], str] = lambda: run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ]
    )
    #: -k is POSIX; BSD df rejects GNU's -B1, which left a Mac with no disks at all.
    df: Callable[[], str] = lambda: run(["df", "-P", "-k"])
    mounts: Callable[[], str] = lambda: run(["mount"])
    vm_stat: Callable[[], str] = lambda: run(["vm_stat"])
    memsize: Callable[[], str] = lambda: run(["sysctl", "-n", "hw.memsize"])
    models: Callable[[int, str, bool], dict[str, Any]] = lambda port, host, tls: probe_models(
        port, host=host, tls=tls
    )
    extra: dict[str, Any] = field(default_factory=dict)


#: A test seam only. Production never sets it: ``active()`` returns live collectors.
_ACTIVE: Collectors | None = None


def active() -> Collectors:
    return _ACTIVE if _ACTIVE is not None else Collectors()


class use:
    """``with collect.use(Collectors(...)):`` reads from transcripts instead of the machine."""

    def __init__(self, collectors: Collectors) -> None:
        self._c = collectors
        self._prev: Collectors | None = None

    def __enter__(self) -> Collectors:
        global _ACTIVE
        self._prev, _ACTIVE = _ACTIVE, self._c
        return self._c

    def __exit__(self, *exc: object) -> None:
        global _ACTIVE
        _ACTIVE = self._prev


def read_disks(c: Collectors) -> list[dict[str, Any]]:
    """The writable disks this machine can report, read through ``c``."""
    return parse_df(c.df(), parse_readonly_mounts(c.mounts()))


__all__ = [
    "Collectors",
    "active",
    "use",
    "ENGINE_PATTERNS",
    "detect_engines",
    "parse_df",
    "parse_models_response",
    "parse_nvidia_smi_apps",
    "parse_nvidia_smi_gpus",
    "parse_ps",
    "parse_readonly_mounts",
    "parse_ss_listeners",
    "parse_systemctl_units",
    "parse_vm_stat",
    "probe_models",
    "read_disks",
    "run",
]
