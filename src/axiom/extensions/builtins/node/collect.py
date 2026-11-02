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
}


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


def parse_df(text: str) -> list[dict[str, Any]]:
    """``df -P -B1`` → real filesystems with bytes (tmpfs, overlays and pseudo filesystems dropped)."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or not parts[1].isdigit():
            continue
        fs, size, used, avail, _pct, mount = (
            parts[0],
            int(parts[1]),
            int(parts[2]),
            int(parts[3]),
            parts[4],
            parts[5],
        )
        if fs in _SKIP_FS or fs.startswith(("tmpfs", "overlay", "/dev/loop")):
            continue
        out.append(
            {
                "filesystem": fs,
                "mount": mount,
                "size_bytes": size,
                "used_bytes": used,
                "free_bytes": avail,
                "used_pct": round(100 * used / size, 1) if size else 0.0,
            }
        )
    return out


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
    df: Callable[[], str] = lambda: run(["df", "-P", "-B1"])
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
    "parse_ss_listeners",
    "parse_systemctl_units",
    "probe_models",
    "run",
]
