"""KEEL reads the machine from what the standard tools print, and says what it saw.

Transcripts below are from a real node on 2026-10-31: a vLLM engine under a
separate account on a TLS port with a key, Ollama on loopback, a LiteLLM
gateway, two GPUs' worth of nvidia-smi lines, and a df with the pseudo
filesystems that must be dropped. Names and ids are generic.
"""

from __future__ import annotations

from axiom.extensions.builtins.node import collect

PS = """\
12058 llm-ser+ 1344156 /home/llm/vllm/.venv/bin/python3 /home/llm/vllm/.venv/bin/vllm serve org/Model-27B-NVFP4 --served-model-name model-27b qwen qwen-coder --dtype auto --max-model-len 262144 --ssl-keyfile /home/llm/host.key --ssl-certfile /home/llm/host.crt --api-key REDACTED --host 0.0.0.0 --port 41883
973795 ollama 960188 /usr/local/bin/ollama serve
1983399 ollama 930155 /usr/local/lib/ollama/llama-server --model /usr/share/ollama/.ollama/models/blobs/sha256-abc --port 40215 --host 127.0.0.1 --no-webui
1793988 svc 1275504 /home/svc/.venv-litellm/bin/python3.12 /home/svc/.venv-litellm/bin/litellm --config /home/svc/.config/serving/litellm.config.yaml --host 0.0.0.0 --port 8700
4180380 svc 6 /home/svc/.venv/bin/python3.12 /home/svc/.venv/bin/uvicorn fused_rag_shim:app --host 127.0.0.1 --port 8767 --workers 8
"""

UNITS = """\
axiom-serve.service          loaded active   running Axiom composed serving app
adopt-recopy.service         loaded failed   failed  Re-copy last windows
ollama.service               loaded active   running Ollama Service
"""

SS = """\
LISTEN 0      2048          0.0.0.0:41883      0.0.0.0:*    users:(("vllm",pid=12058,fd=24))
LISTEN 0      2048          0.0.0.0:8700       0.0.0.0:*    users:(("litellm",pid=1793988,fd=14))
LISTEN 0      4096        127.0.0.1:11434      0.0.0.0:*    users:(("ollama",pid=973795,fd=4))
LISTEN 0      511              [::]:443           [::]:*    users:(("nginx",pid=2396253,fd=8))
"""

GPUS = "0, NVIDIA RTX PRO 6000 Blackwell, 94699, 97887\n"
APPS = "22566, VLLM::EngineCore, 93690\n1983399, /usr/local/lib/ollama/llama-server, 994\n"

DF = """\
Filesystem                   1-blocks         Used    Available Capacity Mounted on
tmpfs                      1000000000    100000000    900000000      10% /run
/dev/mapper/vg-root      264000000000 202000000000  50000000000      81% /
/dev/mapper/vg-home     3800000000000 3300000000000 335000000000      91% /home
/dev/mapper/bulk       20000000000000 15000000000000 3800000000000     81% /bulk
overlay                  264000000000 202000000000  50000000000      81% /var/lib/docker/overlay2/abc/merged
"""


def test_engines_are_detected_with_what_their_command_lines_say():
    engines = collect.detect_engines(collect.parse_ps(PS))
    kinds = {e["kind"]: e for e in engines}
    assert set(kinds) == {"vllm", "ollama", "llama.cpp", "litellm"}
    v = kinds["vllm"]
    assert v["port"] == 41883 and v["host"] == "0.0.0.0"
    assert v["model"] == "org/Model-27B-NVFP4"
    assert v["served_as"] == ["model-27b", "qwen", "qwen-coder"]
    assert v["tls"] is True and v["key_required"] is True
    assert v["user"] == "llm-ser+"
    assert (
        kinds["llama.cpp"]["model"].endswith("sha256-abc") and kinds["llama.cpp"]["port"] == 40215
    )
    assert (
        kinds["litellm"]["config"].endswith("litellm.config.yaml")
        and kinds["litellm"]["port"] == 8700
    )
    # the shim is a plain uvicorn app, not an engine
    assert all(e["pid"] != 4180380 for e in engines)


def test_units_ports_gpu_and_disks_parse_from_the_tools_output():
    units = collect.parse_systemctl_units(UNITS)
    assert [u["unit"] for u in units] == [
        "axiom-serve.service",
        "adopt-recopy.service",
        "ollama.service",
    ]
    assert units[1]["active"] == "failed"
    ports = collect.parse_ss_listeners(SS)
    assert {(p["port"], p["process"]) for p in ports} == {
        (41883, "vllm"),
        (8700, "litellm"),
        (11434, "ollama"),
        (443, "nginx"),
    }
    assert next(p for p in ports if p["port"] == 11434)["address"] == "127.0.0.1"
    gpus = collect.parse_nvidia_smi_gpus(GPUS)
    assert gpus[0]["memory_free_mib"] == 97887 - 94699
    apps = collect.parse_nvidia_smi_apps(APPS)
    assert apps[0]["memory_mib"] == 93690
    disks = collect.parse_df(DF)
    assert [d["mount"] for d in disks] == ["/", "/home", "/bulk"]  # tmpfs and overlay dropped
    assert disks[1]["free_bytes"] == 335000000000 and disks[1]["used_pct"] == 86.8


def test_model_lists_are_read_in_both_shapes():
    assert collect.parse_models_response({"data": [{"id": "qwen"}, {"id": "qwen-coder"}]}) == [
        "qwen",
        "qwen-coder",
    ]
    assert collect.parse_models_response(
        {"models": [{"name": "gemma2:2b"}, {"model": "nomic-embed-text:latest"}]}
    ) == ["gemma2:2b", "nomic-embed-text:latest"]
    assert collect.parse_models_response({"unexpected": 1}) == []


def test_a_missing_tool_is_empty_output_not_a_crash():
    assert collect.run(["definitely-not-a-tool-on-this-machine-xyz"]) == ""
