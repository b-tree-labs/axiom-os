# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""llamafile provisioning — single-binary local LLM.

Downloads and manages a llamafile binary that provides an OpenAI-compatible
API on localhost without Docker, K3D, or any container runtime.

Mozilla project, Apache 2.0 license.

Model profiles:

- ``qwen`` (default) — Qwen2.5 7B Instruct (Q4_K_M), ~4.7GB. Verifier-task
  accuracy 84.9%, p50 latency 481ms. The default since Issue 1 of
  raw-model benchmark support.
- ``small`` — Gemma 2 2B Instruct (Q4_K_M), ~1.6GB. The `simple`-tier /
  as-shipped lightweight default (ADR-054 + spec-llm-tier-policy); minimal
  footprint, fast cold-start. Replaces Bonsai. (Gemma license, not Apache.)
- ``bonsai`` (DEPRECATED) — Bonsai 1.7B GGUF. Degenerate output observed in the
  field; retained only for cache migration. Do not ship as a default.

The qwen GGUF is sourced from the bartowski/Qwen2.5-7B-Instruct-GGUF mirror
(Apache-2.0, single-file Q4_K_M). The official Qwen org packages Q4_K_M as
a 2-shard split which doesn't fit the existing single-file download flow;
bartowski's mirror has the same Apache-2.0 weights as one file.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Model profile registry
# ---------------------------------------------------------------------------

MODELS: dict[str, dict] = {
    "qwen": {
        "url": (
            "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/"
            "resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf"
        ),
        "gguf": "qwen2.5-7b-instruct-q4_k_m.gguf",
        "id": "qwen2.5-7b-instruct",
        "size_gb": 4.7,
        "description": (
            "Qwen 2.5 7B Instruct (Q4_K_M) — verifier-task accuracy 84.9%, "
            "p50 latency 481ms"
        ),
    },
    "small": {
        "url": (
            "https://huggingface.co/bartowski/gemma-2-2b-it-GGUF/"
            "resolve/main/gemma-2-2b-it-Q4_K_M.gguf"
        ),
        "gguf": "gemma-2-2b-it-q4_k_m.gguf",
        "id": "gemma2-2b-it",
        "size_gb": 1.6,
        "description": (
            "Gemma 2 2B Instruct (Q4_K_M) — the `simple`-tier / as-shipped "
            "lightweight default (ADR-054 + spec-llm-tier-policy: 'bundle "
            "gemma2:2b'). ~1.6GB, fast cold-start, minimal footprint. "
            "Supersedes Bonsai 1.7B. NOTE: Gemma license (not Apache) — "
            "bundle the Gemma Terms + Prohibited-Use Policy when shipping."
        ),
    },
    "bonsai": {
        "url": "https://huggingface.co/prism-ml/Bonsai-1.7B-gguf/resolve/main/Bonsai-1.7B.gguf",
        "gguf": "Bonsai-1.7B.gguf",
        "id": "bonsai-1.7b",
        "size_gb": 1.7,
        "description": (
            "Bonsai 1.7B — DEPRECATED. Degenerate output observed in the field "
            "(incoherent completions); superseded by `small` (qwen2.5-3b) per "
            "ADR-054. Retained only for cache migration."
        ),
    },
}

# Small-footprint default for the `simple` tier (replaced bonsai; ADR-054).
SMALL_MODEL = "small"

DEFAULT_MODEL = "qwen"
DEFAULT_LOCAL_MODEL_GGUF = MODELS[DEFAULT_MODEL]["gguf"]
DEFAULT_LOCAL_MODEL_ID = MODELS[DEFAULT_MODEL]["id"]

# Backwards-compat constants — used by older callers and tests. These now
# point at the active default (qwen) but external code should switch to
# DEFAULT_LOCAL_MODEL_GGUF / DEFAULT_LOCAL_MODEL_ID for clarity.
# Canonical source is the Mozilla-Ocho GitHub release. The old HuggingFace
# mirror (Mozilla/llamafile) now 401s, breaking the standard local-LLM install.
LLAMAFILE_URL = "https://github.com/Mozilla-Ocho/llamafile/releases/download/0.8.17/llamafile-0.8.17"
MODEL_URL = MODELS[DEFAULT_MODEL]["url"]
MODEL_NAME = MODELS[DEFAULT_MODEL]["gguf"]
DEFAULT_PORT = 8080


def resolve_model(name: str) -> dict:
    """Return the profile dict for *name* (e.g. ``"qwen"``, ``"bonsai"``).

    Raises ``KeyError`` with a clear message listing valid names.
    """
    try:
        return MODELS[name]
    except KeyError as exc:
        valid = ", ".join(sorted(MODELS.keys()))
        raise KeyError(f"unknown model {name!r} — valid names: {valid}") from exc


def detect_existing_bonsai_cache() -> Path | None:
    """Return the path to a cached Bonsai GGUF if present, else None.

    Used during setup migration so we can offer existing Bonsai users the
    choice to keep their 1.7GB download or upgrade to qwen.
    """
    cached = get_llamafile_dir() / MODELS["bonsai"]["gguf"]
    return cached if cached.exists() else None


def get_llamafile_dir() -> Path:
    """Return ~/.axi/llamafile/, creating it if needed."""
    d = Path.home() / ".axi" / "llamafile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_llamafile_installed(model: str = DEFAULT_MODEL) -> bool:
    """Check if llamafile binary and the requested model GGUF are downloaded."""
    profile = resolve_model(model)
    d = get_llamafile_dir()
    return (d / "llamafile").exists() and (d / profile["gguf"]).exists()


def is_llamafile_running(port: int = DEFAULT_PORT) -> bool:
    """Check if llamafile server is responding on *port*."""
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def download_llamafile(progress_callback=None) -> Path:
    """Download llamafile binary if not present."""
    d = get_llamafile_dir()
    binary = d / "llamafile"

    if binary.exists():
        return binary

    import requests

    if progress_callback:
        progress_callback("Downloading llamafile runtime...")

    resp = requests.get(LLAMAFILE_URL, stream=True, timeout=30)
    resp.raise_for_status()

    with open(binary, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    # Make executable
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return binary


def download_model(progress_callback=None, model: str = DEFAULT_MODEL) -> Path:
    """Download the requested model GGUF if not present."""
    profile = resolve_model(model)
    d = get_llamafile_dir()
    model_path = d / profile["gguf"]

    if model_path.exists():
        return model_path

    import requests

    size = profile.get("size_gb")
    size_label = f"{size}GB" if size else "model"
    if progress_callback:
        progress_callback(f"Downloading {profile['gguf']} ({size_label})...")

    resp = requests.get(profile["url"], stream=True, timeout=30)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0

    with open(model_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
            downloaded += len(chunk)
            if progress_callback and total:
                pct = int(downloaded / total * 100)
                progress_callback(f"Downloading {profile['gguf']}... {pct}%")

    return model_path


def start_llamafile(
    port: int = DEFAULT_PORT,
    background: bool = True,
    model: str = DEFAULT_MODEL,
) -> bool:
    """Start llamafile server.

    Returns True if started successfully.
    """
    if is_llamafile_running(port):
        return True

    profile = resolve_model(model)
    d = get_llamafile_dir()
    binary = d / "llamafile"
    model_path = d / profile["gguf"]

    if not binary.exists() or not model_path.exists():
        return False

    cmd = [
        str(binary),
        "-m",
        str(model_path),
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
        "-c",
        "4096",
        "-t",
        "2",
        "--log-disable",
    ]

    if background:
        # Start as background process
        pid_file = d / "llamafile.pid"
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file.write_text(str(proc.pid))

        # Wait for server to be ready
        import time

        for _ in range(30):  # 30 second timeout
            time.sleep(1)
            if is_llamafile_running(port):
                return True
        return False
    else:
        subprocess.run(cmd, check=True)
        return True


def stop_llamafile() -> bool:
    """Stop the llamafile server."""
    d = get_llamafile_dir()
    pid_file = d / "llamafile.pid"

    if not pid_file.exists():
        return False

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 15)  # SIGTERM
        pid_file.unlink()
        return True
    except (ProcessLookupError, ValueError):
        pid_file.unlink(missing_ok=True)
        return False


def provision(progress_callback=None, model: str = DEFAULT_MODEL) -> dict:
    """Full provisioning: download + start.

    Returns status dict.
    """
    profile = resolve_model(model)
    result = {
        "binary": False,
        "model": False,
        "running": False,
        "port": DEFAULT_PORT,
        "model_id": profile["id"],
        "model_gguf": profile["gguf"],
    }

    try:
        download_llamafile(progress_callback)
        result["binary"] = True
    except Exception as e:
        result["error"] = f"Failed to download llamafile: {e}"
        return result

    try:
        download_model(progress_callback, model=model)
        result["model"] = True
    except Exception as e:
        result["error"] = f"Failed to download model: {e}"
        return result

    try:
        started = start_llamafile(model=model)
        result["running"] = started
        if not started:
            result["error"] = "llamafile started but server not responding"
    except Exception as e:
        result["error"] = f"Failed to start llamafile: {e}"

    return result


def get_status(model: str = DEFAULT_MODEL) -> dict:
    """Get current llamafile status."""
    profile = resolve_model(model)
    return {
        "installed": is_llamafile_installed(model=model),
        "running": is_llamafile_running(),
        "port": DEFAULT_PORT,
        "model": profile["gguf"],
        "model_id": profile["id"],
        "path": str(get_llamafile_dir()),
    }
