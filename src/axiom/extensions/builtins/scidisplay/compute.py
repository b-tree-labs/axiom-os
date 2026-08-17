# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Deterministic computation kernel — Sci Displays Pillar 2 (A6).

Per ADR-039 D2 + spec §7: the LLM proposes equations, this kernel
*verifies* — the asymmetric edge. Every numerical result carries a
SymPy AST trail (provenance hash) so the user sees both the equation
and the computed answer with a guarantee that the computation
actually ran (not LLM-hallucinated).

Architecture:

  caller →  ``compute(latex, mode=...)``
              ↓
            subprocess.Popen(
              ``python -m axiom.extensions.builtins.scidisplay.compute_worker``
              + pickled input via stdin
              ↓
            worker imports SymPy / NumPy / mpmath; runs computation
              ↓
            pickled result via stdout
              ↓
          ``ComputationResult``  with provenance receipt + AST trail

The chat process never `exec`s user-touched code. Subprocess isolation
is the Phase A boundary; OS-level confinement (Seatbelt on macOS,
seccomp+unshare on Linux) is the Phase B hardening pass per ADR-039 D3.

For long-running jobs (>2s expected per the cheap heuristic in
``estimate_runtime``), the caller can dispatch via the background-tasks
primitive (``axiom.infra.tasks``) so the chat surface stays responsive.
"""

from __future__ import annotations

import hashlib
import logging
import pickle
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

ComputeMode = Literal["symbolic", "numeric", "arbitrary"]
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_PRECISION_DIGITS = 50  # for arbitrary-precision mode


@dataclass(frozen=True)
class ComputationRequest:
    latex: str
    mode: ComputeMode = "symbolic"
    precision: int = DEFAULT_PRECISION_DIGITS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    # Cross-NODE compute: when set, SSH-dispatch the kernel to this peer
    # (federation directory display_name, e.g. "<host>:<user>"). The peer
    # signs the result with its Ed25519 keypair; the originating node
    # verifies the signature against the peer's pubkey from the registry.
    peer: str | None = None


@dataclass(frozen=True)
class ComputationResult:
    """Outcome of one compute pass.

    Successful results carry both ``value_repr`` (the displayable answer)
    and ``ast_trail`` (the SymPy AST string form — the provenance proof
    that something deterministic computed it). The receipt id hashes
    the (latex, mode, value_repr) triple so identical requests always
    produce identical receipts; this is the cache key for re-runs.

    Cross-NODE compute additions (when ``executed_on_peer`` is set):

    - ``executed_on_peer``: the federation peer that ran the computation
      (e.g. "<host>:<user>"). ``None`` when run locally.
    - ``signed_by_node_id`` / ``signed_by_display_name`` / ``signing_pubkey_b64`` /
      ``signature_b64``: cryptographic attestation that the executing peer
      *did* compute this answer. Verifier on the originating side checks
      the signature against the peer's pubkey from the federation directory.
    - ``signature_valid``: result of that verification.
    - ``signature_verification_reason``: when invalid, why.
    """

    request: ComputationRequest
    success: bool
    value_repr: str = ""
    ast_trail: str = ""
    error_class: str = ""
    error_message: str = ""
    receipt_id: str = ""
    elapsed_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)
    # Cross-node attestation (None when local-only)
    executed_on_peer: str | None = None
    signed_by_node_id: str = ""
    signed_by_display_name: str = ""
    signing_pubkey_b64: str = ""
    signature_b64: str = ""
    signature_valid: bool | None = None
    signature_verification_reason: str = ""


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def _receipt_id(latex: str, mode: str, value_repr: str) -> str:
    h = hashlib.sha256(f"{mode}|{latex}|{value_repr}".encode("utf-8")).hexdigest()[:16]
    return f"axiom://compute/{h}"


# ---------------------------------------------------------------------------
# Subprocess invocation
# ---------------------------------------------------------------------------


def compute(req: ComputationRequest) -> ComputationResult:
    """Run a computation in an isolated subprocess. Never raises.

    Failure cases (timeout, parse error, runtime error) all return a
    ComputationResult with success=False and error fields populated so
    the caller can show a typed failure to the user (and the LLM can
    reason about the next step).

    When ``req.peer`` is set, SSH-dispatch to that federation peer
    (cross-NODE compute). Result is verified against the peer's pubkey
    from the federation directory; verification result is stamped into
    ``signature_valid``.
    """
    if req.peer:
        return _compute_via_peer(req)
    import time

    start = time.monotonic()
    payload = pickle.dumps({
        "latex": req.latex,
        "mode": req.mode,
        "precision": req.precision,
    })
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "axiom.extensions.builtins.scidisplay.compute_worker"],
            input=payload,
            capture_output=True,
            timeout=req.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        elapsed = (time.monotonic() - start) * 1000
        return ComputationResult(
            request=req,
            success=False,
            error_class="TimeoutExpired",
            error_message=f"compute exceeded {req.timeout_seconds}s budget",
            elapsed_ms=elapsed,
        )
    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        return ComputationResult(
            request=req,
            success=False,
            error_class=type(exc).__name__,
            error_message=str(exc),
            elapsed_ms=elapsed,
        )

    elapsed = (time.monotonic() - start) * 1000

    if proc.returncode != 0:
        return ComputationResult(
            request=req,
            success=False,
            error_class="WorkerExitNonZero",
            error_message=(proc.stderr.decode("utf-8", errors="replace")[:500]
                           if proc.stderr else f"exit code {proc.returncode}"),
            elapsed_ms=elapsed,
        )

    try:
        result = pickle.loads(proc.stdout)
    except Exception as exc:
        return ComputationResult(
            request=req,
            success=False,
            error_class="UnpickleError",
            error_message=str(exc),
            elapsed_ms=elapsed,
        )

    if not result.get("success"):
        return ComputationResult(
            request=req,
            success=False,
            error_class=result.get("error_class", "Unknown"),
            error_message=result.get("error_message", ""),
            elapsed_ms=elapsed,
        )

    value_repr = result.get("value_repr", "")
    ast_trail = result.get("ast_trail", "")

    return ComputationResult(
        request=req,
        success=True,
        value_repr=value_repr,
        ast_trail=ast_trail,
        receipt_id=_receipt_id(req.latex, req.mode, value_repr),
        elapsed_ms=elapsed,
        extra=result.get("extra", {}),
    )


# ---------------------------------------------------------------------------
# Cross-NODE compute via SSH dispatch
# ---------------------------------------------------------------------------


# Self-contained inline worker — runs on the remote peer via SSH-stdin.
# Imports ONLY sympy + cryptography (no axiom). Reads the request as a
# base64 literal substituted in at dispatch time. Writes the pickled +
# base64-wrapped response on a single line marked with the response
# prefix, so unrelated stdout noise (banners, warnings) is filterable.
#
# The signing keypair is loaded from ``~/.axi/identity/private.pem``,
# which any node initialised via ``axi federation init`` already has.
# The response carries the peer's node_id + pubkey + signature so the
# originating node can verify against its own copy of the peer's pubkey
# from the federation directory.
_REMOTE_SIGNED_WORKER_TEMPLATE = r'''
import base64, hashlib, json, os, pickle, sys, time, traceback
from pathlib import Path

REQUEST_B64 = "{request_b64}"

def _try_parse_latex(latex):
    try:
        from sympy.parsing.latex import parse_latex
        return parse_latex(latex)
    except Exception:
        return None

def _try_parse_sympy(s):
    try:
        from sympy import sympify
        return sympify(s)
    except Exception:
        return None

def _compute_symbolic(latex):
    expr = _try_parse_latex(latex) or _try_parse_sympy(latex)
    if expr is None:
        return {{"success": False, "error_class": "ParseError",
                 "error_message": f"could not parse {{latex!r}}"}}
    try:
        from sympy import simplify
        s = simplify(expr)
    except Exception as e:
        return {{"success": False, "error_class": type(e).__name__,
                 "error_message": str(e)}}
    return {{"success": True, "value_repr": str(s), "ast_trail": repr(s)}}

def _compute_numeric(latex):
    expr = _try_parse_latex(latex) or _try_parse_sympy(latex)
    if expr is None:
        return {{"success": False, "error_class": "ParseError",
                 "error_message": f"could not parse {{latex!r}}"}}
    try:
        v = float(expr.evalf())
    except (TypeError, ValueError):
        try:
            u = expr.evalf()
            return {{"success": True, "value_repr": str(u), "ast_trail": repr(u)}}
        except Exception as e:
            return {{"success": False, "error_class": type(e).__name__,
                     "error_message": str(e)}}
    return {{"success": True, "value_repr": repr(v),
             "ast_trail": f"float(({{expr!r}}).evalf())"}}

def _compute_arbitrary(latex, precision):
    expr = _try_parse_latex(latex) or _try_parse_sympy(latex)
    if expr is None:
        return {{"success": False, "error_class": "ParseError",
                 "error_message": f"could not parse {{latex!r}}"}}
    try:
        v = expr.evalf(precision)
    except Exception as e:
        return {{"success": False, "error_class": type(e).__name__,
                 "error_message": str(e)}}
    return {{"success": True, "value_repr": str(v),
             "ast_trail": f"({{expr!r}}).evalf({{precision}})"}}

def _emit(payload):
    blob = base64.b64encode(pickle.dumps(payload)).decode("ascii")
    sys.stdout.write("AXIOM_COMPUTE_RESPONSE:" + blob + "\n")
    sys.stdout.flush()

def main():
    try:
        req = pickle.loads(base64.b64decode(REQUEST_B64))
    except Exception as e:
        _emit({{"success": False, "error_class": "InputDecodeError",
                "error_message": str(e)}})
        return 0

    latex = req.get("latex", "")
    mode = req.get("mode", "symbolic")
    precision = int(req.get("precision", 50))

    t0 = time.monotonic()
    try:
        if mode == "symbolic":
            inner = _compute_symbolic(latex)
        elif mode == "numeric":
            inner = _compute_numeric(latex)
        elif mode == "arbitrary":
            inner = _compute_arbitrary(latex, precision)
        else:
            inner = {{"success": False, "error_class": "UnknownMode",
                      "error_message": f"unknown mode {{mode!r}}"}}
    except Exception as e:
        inner = {{"success": False, "error_class": type(e).__name__,
                  "error_message": str(e),
                  "extra": {{"traceback": traceback.format_exc()[:500]}}}}
    elapsed_ms = (time.monotonic() - t0) * 1000

    if not inner["success"]:
        _emit({{"success": False,
                "error_class": inner.get("error_class"),
                "error_message": inner.get("error_message")}})
        return 0

    # ---- Sign the canonical (latex|mode|value|ast) tuple ----
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, PublicFormat, NoEncryption,
        )

        identity_dir = Path.home() / ".axi" / "identity"
        meta = json.loads((identity_dir / "identity.json").read_text())
        pem = (identity_dir / "private.pem").read_bytes()
        priv = serialization.load_pem_private_key(pem, password=None)
        pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

        canonical = f"{{mode}}|{{latex}}|{{inner['value_repr']}}|{{inner['ast_trail']}}".encode("utf-8")
        sig = priv.sign(canonical)
        canonical_hash = hashlib.sha256(canonical).hexdigest()

        signed_dict = {{
            "latex": latex,
            "mode": mode,
            "precision": precision,
            "value_repr": inner["value_repr"],
            "ast_trail": inner["ast_trail"],
            "signing_node_id": meta["node_id"],
            "signing_node_display_name": meta["display_name"],
            "signing_pubkey_b64": base64.b64encode(pub_bytes).decode("ascii"),
            "signature_b64": base64.b64encode(sig).decode("ascii"),
            "canonical_hash": canonical_hash,
            "elapsed_ms": elapsed_ms,
        }}
    except Exception as e:
        _emit({{"success": False, "error_class": "SigningError",
                "error_message": f"{{type(e).__name__}}: {{e}}",
                "extra": {{"traceback": traceback.format_exc()[:500]}}}})
        return 0

    _emit({{"success": True, "signed_result": signed_dict}})
    return 0

sys.exit(main())
'''



def _compute_via_peer(req: ComputationRequest) -> ComputationResult:
    """Dispatch the compute kernel to a federation peer via SSH, then verify
    the peer's signature against its pubkey from the federation directory.

    Failure modes (all return success=False, never raise):
      - peer not found in registry
      - peer not reachable / SSH error
      - peer's worker errored / timed out
      - signature did not verify against expected pubkey
    """
    import time

    start = time.monotonic()
    request_id = hashlib.sha256(
        f"{time.time_ns()}|{req.latex}".encode("utf-8")
    ).hexdigest()[:16]

    # Look up peer in federation directory.
    try:
        from axiom.vega.federation.discovery import NodeRegistry

        reg = NodeRegistry()
        peer_record = next(
            (p for p in reg.list_all() if p.display_name == req.peer),
            None,
        )
    except Exception as exc:
        return ComputationResult(
            request=req,
            success=False,
            error_class="RegistryError",
            error_message=f"could not load NodeRegistry: {exc}",
            executed_on_peer=req.peer,
            elapsed_ms=(time.monotonic() - start) * 1000,
        )
    if peer_record is None:
        return ComputationResult(
            request=req,
            success=False,
            error_class="UnknownPeer",
            error_message=f"no federation peer with display_name={req.peer!r}",
            executed_on_peer=req.peer,
            elapsed_ms=(time.monotonic() - start) * 1000,
        )
    if not peer_record.public_key:
        return ComputationResult(
            request=req,
            success=False,
            error_class="PeerPubkeyMissing",
            error_message=(
                f"peer {req.peer!r} has no public_key on file; "
                "run `axi nodes verify` to TOFU it first"
            ),
            executed_on_peer=req.peer,
            elapsed_ms=(time.monotonic() - start) * 1000,
        )

    ssh_user = getattr(peer_record, "ssh_user", "") or ""
    ssh_host = getattr(peer_record, "ssh_host", "") or ""
    if not (ssh_user and ssh_host):
        return ComputationResult(
            request=req,
            success=False,
            error_class="PeerSSHMissing",
            error_message=(
                f"peer {req.peer!r} has no ssh_user/ssh_host on record; "
                f"only ssh:// URLs are dispatchable today"
            ),
            executed_on_peer=req.peer,
            elapsed_ms=(time.monotonic() - start) * 1000,
        )

    # Build a self-contained remote program. The peer doesn't need our
    # ``compute_worker_signed`` module (avoids the "must update the remote peer
    # first" deploy chain). Requires only:
    #   - python3 on PATH (any reasonable version)
    #   - sympy + cryptography (both standard axi deps; pre-installed on
    #     any node already running axi).
    # The request is embedded as a base64 literal in the program text so
    # we have ONE stdin channel — the program — and ONE stdout channel —
    # the (base64-wrapped) pickled response.
    import base64

    request_payload = pickle.dumps({
        "latex": req.latex,
        "mode": req.mode,
        "precision": req.precision,
        "request_id": request_id,
    })
    request_b64 = base64.b64encode(request_payload).decode("ascii")
    remote_program = _REMOTE_SIGNED_WORKER_TEMPLATE.format(
        request_b64=request_b64,
    )

    ssh_argv = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        f"{ssh_user}@{ssh_host}",
        # /usr/bin/python3 is reliably on every Linux node; the inline
        # program imports only sympy + cryptography (no axiom). bash -lc
        # would also work but adds noise (~/.profile output that some
        # systems emit). Direct python3 invocation keeps stdout clean.
        "/usr/bin/python3",
    ]
    payload = remote_program.encode("utf-8")

    try:
        proc = subprocess.run(
            ssh_argv,
            input=payload,
            capture_output=True,
            timeout=req.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return ComputationResult(
            request=req,
            success=False,
            error_class="TimeoutExpired",
            error_message=f"peer compute exceeded {req.timeout_seconds}s budget",
            executed_on_peer=req.peer,
            elapsed_ms=(time.monotonic() - start) * 1000,
        )
    except Exception as exc:
        return ComputationResult(
            request=req,
            success=False,
            error_class="SSHDispatchError",
            error_message=f"{type(exc).__name__}: {exc}",
            executed_on_peer=req.peer,
            elapsed_ms=(time.monotonic() - start) * 1000,
        )

    elapsed = (time.monotonic() - start) * 1000

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[:500] if proc.stderr else ""
        return ComputationResult(
            request=req,
            success=False,
            error_class="PeerWorkerExitNonZero",
            error_message=f"exit {proc.returncode}: {stderr}",
            executed_on_peer=req.peer,
            elapsed_ms=elapsed,
        )

    # Remote program prints the response as one base64 line prefixed
    # ``AXIOM_COMPUTE_RESPONSE:`` so any unrelated stdout noise (login
    # banners, deprecation warnings) is easy to filter.
    import base64

    response_b64: str | None = None
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("AXIOM_COMPUTE_RESPONSE:"):
            response_b64 = line[len("AXIOM_COMPUTE_RESPONSE:"):].strip()
            break
    if response_b64 is None:
        return ComputationResult(
            request=req,
            success=False,
            error_class="ResponseMarkerMissing",
            error_message=(
                "remote program produced no AXIOM_COMPUTE_RESPONSE line; "
                f"stdout head: {proc.stdout[:300]!r}"
            ),
            executed_on_peer=req.peer,
            elapsed_ms=elapsed,
        )

    try:
        wire = pickle.loads(base64.b64decode(response_b64))
    except Exception as exc:
        return ComputationResult(
            request=req,
            success=False,
            error_class="UnpickleError",
            error_message=f"{type(exc).__name__}: {exc}",
            executed_on_peer=req.peer,
            elapsed_ms=elapsed,
        )

    if not wire.get("success"):
        return ComputationResult(
            request=req,
            success=False,
            error_class=wire.get("error_class", "Unknown"),
            error_message=wire.get("error_message", ""),
            executed_on_peer=req.peer,
            elapsed_ms=elapsed,
        )

    # Verify the peer's signature against the pubkey we expected.
    from .compute_signing import SignedComputeResult, verify_signed_result

    try:
        signed = SignedComputeResult(**wire["signed_result"])
    except Exception as exc:
        return ComputationResult(
            request=req,
            success=False,
            error_class="SignedResultDecodeError",
            error_message=str(exc),
            executed_on_peer=req.peer,
            elapsed_ms=elapsed,
        )

    verification = verify_signed_result(
        signed,
        expected_pubkey_b64=peer_record.public_key,
        expected_node_id=peer_record.node_id,
    )

    return ComputationResult(
        request=req,
        success=True,
        value_repr=signed.value_repr,
        ast_trail=signed.ast_trail,
        receipt_id=_receipt_id(req.latex, req.mode, signed.value_repr),
        elapsed_ms=elapsed,
        extra={
            "peer_elapsed_ms": signed.elapsed_ms,
            "canonical_hash": signed.canonical_hash,
            "request_id": request_id,
        },
        executed_on_peer=req.peer,
        signed_by_node_id=signed.signing_node_id,
        signed_by_display_name=signed.signing_node_display_name,
        signing_pubkey_b64=signed.signing_pubkey_b64,
        signature_b64=signed.signature_b64,
        signature_valid=verification.valid,
        signature_verification_reason=verification.reason,
    )


# ---------------------------------------------------------------------------
# Cost estimator (cheap deterministic — never an LLM call)
# ---------------------------------------------------------------------------


def estimate_runtime_seconds(req: ComputationRequest) -> float:
    """Cheap heuristic for "should this go to background-tasks?".

    Per spec §7.4: we want to background anything > 2s expected so the
    chat surface stays responsive. Heuristic uses input length + mode
    + precision; conservative on the "background it" side.
    """
    base = 0.05  # Per-call overhead (subprocess fork + pickle).
    expr_complexity = len(req.latex) / 200.0  # 200-char expressions trip ~1s.
    mode_multiplier = {
        "symbolic": 1.0,
        "numeric": 0.3,
        "arbitrary": max(1.0, req.precision / 50.0),
    }.get(req.mode, 1.0)
    # Sub-expressions that hint expensive work.
    keyword_hits = sum(
        1 for kw in ("integrate", "solve", "dsolve", "limit", "diff", "series",
                     "factor", "expand", "simplify")
        if kw in req.latex.lower()
    )
    return base + expr_complexity * mode_multiplier + keyword_hits * 0.5


__all__ = [
    "ComputationRequest",
    "ComputationResult",
    "ComputeMode",
    "compute",
    "estimate_runtime_seconds",
]
