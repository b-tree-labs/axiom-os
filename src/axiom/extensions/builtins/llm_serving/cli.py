# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""`axi serving` — install and operate the vLLM + LiteLLM + RAG serving stack.

Turns the hand-run pivot deployment into an installable, drift-detectable revision:
`install` renders systemd --user units + configs from [defaults] (overridable by
env / runtime config), so the running processes are managed services, not nohup.

Verbs: install | start | stop | restart | status | diagnose | smoke | uninstall
All values are config — nothing facility-specific is baked in (ADR domain-agnostic).
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import ssl
from pathlib import Path

HERE = Path(__file__).parent
CFG_DIR = Path(os.environ.get("AXIOM_SERVING_CFG", Path.home() / ".config/axiom-serving"))
UNIT_DIR = Path.home() / ".config/systemd/user"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def load_config() -> dict:
    """Manifest [defaults], overlaid by AXIOM_SERVING_* env, overlaid by config.json."""
    with open(HERE / "axiom-extension.toml", "rb") as f:
        cfg = dict(tomllib.load(f).get("defaults", {}))
    fpath = CFG_DIR / "config.json"
    if fpath.exists():
        cfg.update(json.loads(fpath.read_text()))
    for k in list(cfg):
        env = os.environ.get("AXIOM_SERVING_" + k.upper())
        if env is not None:
            cfg[k] = type(cfg[k])(env) if not isinstance(cfg[k], bool) else env.lower() == "true"
    return cfg


def render_litellm_config(c: dict) -> str:
    return f"""# rendered by axi serving install — do not hand-edit
model_list:
  - model_name: {c['gateway_alias']}
    litellm_params:
      model: openai/{c['served_model_name']}
      api_base: http://127.0.0.1:{c['vllm_port']}/v1
      api_key: "none"
  - model_name: {c['served_model_name']}
    litellm_params:
      model: openai/{c['served_model_name']}
      api_base: http://127.0.0.1:{c['vllm_port']}/v1
      api_key: "none"
litellm_settings:
  success_callback: ["{c['observability']}"]
  failure_callback: ["{c['observability']}"]
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"""


def unit(name: str, desc: str, execstart: str, env: dict, after: str = "network.target") -> str:
    envlines = "\n".join(f'Environment="{k}={v}"' for k, v in env.items())
    return f"""[Unit]
Description={desc}
After={after}

[Service]
Type=simple
{envlines}
ExecStart={execstart}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def cmd_install(c: dict, args):
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    venv_vllm = os.environ.get("AXIOM_VLLM_VENV", str(Path.home() / ".venv-vllm"))
    venv_gw = os.environ.get("AXIOM_GATEWAY_VENV", str(Path.home() / ".venv-litellm"))
    tls_crt = os.environ.get("AXIOM_TLS_CRT", "")
    tls_key = os.environ.get("AXIOM_TLS_KEY", "")

    (CFG_DIR / "litellm.config.yaml").write_text(render_litellm_config(c))
    shutil.copy(HERE / "rag_shim.py", CFG_DIR / "rag_shim.py")

    # vLLM engine (internal)
    (UNIT_DIR / "axiom-vllm.service").write_text(unit(
        "axiom-vllm", "Axiom vLLM serving engine",
        f"{venv_vllm}/bin/vllm serve {c['model']} --host 127.0.0.1 --port {c['vllm_port']} "
        f"--served-model-name {c['served_model_name']} --gpu-memory-utilization {c['vllm_gpu_mem_util']} "
        f"--max-num-seqs {c['vllm_max_num_seqs']} --max-model-len {c['vllm_max_model_len']}",
        {"HF_HOME": os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface"))}))

    # LiteLLM gateway (client-facing; TLS if cert provided)
    tls = f" --ssl_keyfile_path {tls_key} --ssl_certfile_path {tls_crt}" if tls_crt and tls_key else ""
    gw_env = {"LITELLM_MASTER_KEY": os.environ.get("LITELLM_MASTER_KEY", "change-me"),
              "LANGFUSE_HOST": os.environ.get("LANGFUSE_HOST", ""),
              "LANGFUSE_PUBLIC_KEY": os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
              "LANGFUSE_SECRET_KEY": os.environ.get("LANGFUSE_SECRET_KEY", "")}
    (UNIT_DIR / "axiom-litellm.service").write_text(unit(
        "axiom-litellm", "Axiom LiteLLM gateway",
        f"{venv_gw}/bin/litellm --config {CFG_DIR}/litellm.config.yaml "
        f"--host 0.0.0.0 --port {c['gateway_port']}{tls}",
        gw_env, after="axiom-vllm.service"))

    # RAG shim
    rag_env = {"RAG_DB_URL": os.environ.get("RAG_DB_URL", os.environ.get("DATABASE_URL", "")),
               "RAG_OLLAMA": os.environ.get("RAG_OLLAMA", "http://localhost:11434"),
               "RAG_EMBED_MODEL": c["embed_model"],
               "RAG_LITELLM_URL": os.environ.get("AXIOM_GATEWAY_URL", f"https://localhost:{c['gateway_port']}"),
               "RAG_LITELLM_KEY": os.environ.get("LITELLM_MASTER_KEY", "change-me"),
               "RAG_GEN_MODEL": c["gateway_alias"], "RAG_TOP_K": c["rag_top_k"],
               "RAG_MIN_SCORE": c["rag_min_score"], "RAG_CHUNK_CHARS": c["rag_chunk_chars"]}
    (UNIT_DIR / "axiom-rag.service").write_text(unit(
        "axiom-rag", "Axiom RAG completion endpoint",
        f"{venv_gw}/bin/uvicorn rag_shim:app --host 0.0.0.0 --port {c['rag_port']} "
        f"--workers 2 --app-dir {CFG_DIR}",
        rag_env, after="axiom-litellm.service"))

    _systemctl("daemon-reload")
    for s in ("axiom-vllm", "axiom-litellm", "axiom-rag"):
        _systemctl("enable", s + ".service")
    print(f"installed units to {UNIT_DIR} + config to {CFG_DIR}")
    print("start with: axi serving start")


def _systemctl(*a):
    return subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True)


def cmd_start(c, args):
    for s in ("axiom-vllm", "axiom-litellm", "axiom-rag"):
        print(s, _systemctl("start", s + ".service").returncode == 0 and "started" or "FAILED")


def cmd_stop(c, args):
    for s in ("axiom-rag", "axiom-litellm", "axiom-vllm"):
        _systemctl("stop", s + ".service")
    print("stopped")


def cmd_restart(c, args):
    cmd_stop(c, args)
    cmd_start(c, args)


def _get(url, headers=None):
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=6, context=CTX) as r:
            return r.status
    except Exception as e:
        return f"ERR {type(e).__name__}"


def cmd_status(c, args):
    print("=== units ===")
    for s in ("axiom-vllm", "axiom-litellm", "axiom-rag"):
        print(f"  {s}: {_systemctl('is-active', s + '.service').stdout.strip()}")
    print("=== endpoints ===")
    vllm_h = _get("http://127.0.0.1:%s/v1/models" % c["vllm_port"])
    gw_h = _get("https://localhost:%s/health/liveliness" % c["gateway_port"])
    rag_h = _get("http://localhost:%s/health" % c["rag_port"])
    print(f"  vllm   :{c['vllm_port']}  /v1/models = {vllm_h}")
    print(f"  gateway:{c['gateway_port']} /health   = {gw_h}")
    print(f"  rag    :{c['rag_port']}  /health   = {rag_h}")


def cmd_diagnose(c, args):
    """Codify the hot-deploy lessons as preflight checks (#15)."""
    ok = True
    # GPU driver/library mismatch (the real incident root cause)
    smi = subprocess.run(["nvidia-smi"], capture_output=True, text=True)
    if smi.returncode != 0:
        ok = False
        print("  [FAIL] nvidia-smi failing — driver/library mismatch. Fix without reboot:")
        print("         sudo rmmod nvidia_uvm nvidia_drm nvidia_modeset nvidia && sudo modprobe nvidia nvidia_uvm")
    else:
        print("  [ok] GPU visible")
    # vLLM Blackwell pin
    v = subprocess.run([os.environ.get("AXIOM_VLLM_VENV", str(Path.home() / ".venv-vllm")) + "/bin/python",
                        "-c", "import torch,vllm;print(torch.__version__, vllm.__version__, torch.version.cuda)"],
                       capture_output=True, text=True)
    print(f"  [info] torch/vllm/cuda: {v.stdout.strip() or v.stderr.strip()[:80]}")
    # model present
    model_ok = any((Path.home() / ".cache/huggingface").glob(f"**/*{c['model'].split('/')[-1][:8]}*")) \
        if (Path.home() / ".cache/huggingface").exists() else False
    print(f"  [{'ok' if model_ok else 'warn'}] model cache for {c['model']}")
    # langfuse reachable
    lh = os.environ.get("LANGFUSE_HOST", "")
    print(f"  [info] langfuse {lh or '(unset)'}: {_get(lh + '/api/public/health') if lh else 'n/a'}")
    # ports free / owned
    for p in (c["vllm_port"], c["gateway_port"], c["rag_port"]):
        print(f"  [info] port {p}: {'in-use' if _port_in_use(p) else 'free'}")
    sys.exit(0 if ok else 1)


def _port_in_use(p):
    import socket
    s = socket.socket()
    r = s.connect_ex(("127.0.0.1", int(p)))
    s.close()
    return r == 0


def _urllib_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def _urllib_post(url, headers=None, json=None):
    import json as _json
    data = _json.dumps(json or {}).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
            return r.status, _json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:  # surface status, don't raise
        body = e.read().decode() if hasattr(e, "read") else ""
        try:
            return e.code, _json.loads(body or "{}")
        except Exception:
            return e.code, {"error": body[:200]}


def cmd_smoke(c, args):
    """Serving-contract smoke — the cutover + CI gate (#49).

    Reasoning-aware + latency-sized: a slow-but-correct answer passes (warns);
    only a missing/empty/errored/timed-out answer rolls back. Exit 0 on
    pass (HEALTHY|SLOW), 1 on BROKEN — so it drops straight into
    stage→smoke→flip→smoke→rollback and CI.
    """
    import time
    from axiom.extensions.builtins.llm_serving.contract import run_contract_smoke

    base = os.environ.get("AXIOM_SERVING_SMOKE_URL",
                          "https://localhost:%s" % c["gateway_port"])
    model = os.environ.get("AXIOM_SERVING_SMOKE_MODEL", c.get("model", "qwen"))
    key = os.environ.get("LITELLM_MASTER_KEY", os.environ.get("AXIOM_API_KEY", ""))
    timeout_s = float(os.environ.get("AXIOM_SERVING_SMOKE_TIMEOUT", "60"))
    warn_s = float(os.environ.get("AXIOM_SERVING_SMOKE_WARN", "20"))

    result = run_contract_smoke(
        base, model=model, http_get=_urllib_get, http_post=_urllib_post,
        clock=time.monotonic, timeout_s=timeout_s, warn_latency_s=warn_s,
        api_key=key,
    )
    print(result.summary())
    sys.exit(0 if result.passed else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="axi serving")
    sub = ap.add_subparsers(dest="verb", required=True)
    for v in ("install", "start", "stop", "restart", "status", "diagnose", "smoke", "uninstall"):
        sub.add_parser(v)
    args = ap.parse_args(argv)
    c = load_config()
    {"install": cmd_install, "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
     "status": cmd_status, "diagnose": cmd_diagnose, "smoke": cmd_smoke,
     "uninstall": cmd_stop}[args.verb](c, args)


if __name__ == "__main__":
    main()
