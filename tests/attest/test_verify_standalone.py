# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The standalone verifier ships inside every evidence package. A regulator runs
it with nothing but Python: no Axiom, no third-party packages, no secret. These
tests hold it to that and prove it agrees with the platform (ADR-143)."""

from __future__ import annotations

import ast
import base64
import json
import subprocess
import sys
from pathlib import Path

from axiom.attest import tools
from axiom.attest.chain import GENESIS, Ed25519Signer, seal
from axiom.vega.identity.keypair import generate_keypair

VERIFY = Path(tools.__file__).parent / "verify.py"
VECTORS = Path(__file__).parent / "vectors" / "canonical.json"


def _load_standalone():
    import importlib.util

    spec = importlib.util.spec_from_file_location("attest_verify_standalone", VERIFY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_imports_only_the_standard_library():
    tree = ast.parse(VERIFY.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    assert names <= stdlib, f"non-stdlib imports: {sorted(names - stdlib)}"


def test_rfc8032_test_vector_1():
    v = _load_standalone()
    public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555"
        "fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert v.ed25519_verify(public, b"", sig)
    assert not v.ed25519_verify(public, b"x", sig)


def test_pure_python_ed25519_agrees_with_the_platform_library():
    v = _load_standalone()
    for i in range(5):
        kp = generate_keypair()
        msg = f"message {i}".encode()
        sig = kp.sign(msg)
        assert v.ed25519_verify(kp.public_bytes, msg, sig)
        assert not v.ed25519_verify(kp.public_bytes, msg + b"!", sig)


def test_standalone_canonical_form_matches_the_shared_vectors():
    v = _load_standalone()
    for vec in json.loads(VECTORS.read_text(encoding="utf-8")):
        record = v.decode_tagged(vec["record"])
        signed = {k: x for k, x in record.items() if k not in v.SIGNATURE_FIELDS}
        assert v.canonical_bytes(signed).decode("utf-8") == vec["canonical"], vec["name"]
        assert v.digest(record) == vec["digest"], vec["name"]


def _write_package(tmp_path: Path, n: int = 3) -> tuple[Path, Path, list[dict]]:
    signer = Ed25519Signer(key_id="node-key-1", keypair=generate_keypair())
    prev = GENESIS
    records = []
    for i in range(1, n + 1):
        rec = seal({"content": {"title": f"Round {i}"}}, seq=i, prev_digest=prev, signer=signer)
        records.append(rec)
        prev = rec["digest"]
    rec_path = tmp_path / "records.jsonl"
    rec_path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    keys_path = tmp_path / "keys.json"
    keys_path.write_text(
        json.dumps({"node-key-1": base64.b64encode(signer.public_bytes).decode()}), encoding="utf-8"
    )
    return rec_path, keys_path, records


def test_cli_accepts_an_intact_package(tmp_path):
    rec_path, keys_path, records = _write_package(tmp_path)
    out = subprocess.run(
        [sys.executable, str(VERIFY), str(rec_path), "--keys", str(keys_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "OK" in out.stdout
    assert records[-1]["digest"] in out.stdout


def test_cli_reports_the_first_broken_record(tmp_path):
    rec_path, keys_path, records = _write_package(tmp_path)
    records[1]["content"]["title"] = "edited"
    rec_path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(VERIFY), str(rec_path), "--keys", str(keys_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 1
    assert "seq 2" in out.stdout
    assert "digest_mismatch" in out.stdout
