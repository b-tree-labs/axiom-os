# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Compute kernel worker with Ed25519 signing — runs on the executing peer.

Wraps ``compute_worker.main()`` with the local node's signature so the
result is verifiable without re-running the computation. Used by the
SSH-dispatched cross-NODE compute path: laptop sends the request via
SSH; the remote peer runs this worker; its signed payload comes back; laptop
verifies against the peer's pubkey from the federation directory.

Wire format (stdin → stdout, both pickled):

  REQUEST:
    {"latex": str, "mode": str, "precision": int, "request_id": str}

  RESPONSE (success):
    {"success": True, "signed_result": SignedComputeResult-as-dict, ...}

  RESPONSE (failure):
    {"success": False, "error_class": str, "error_message": str}
"""

from __future__ import annotations

import pickle
import sys
import time
import traceback


def main() -> int:
    try:
        payload = pickle.loads(sys.stdin.buffer.read())
    except Exception as exc:
        sys.stdout.buffer.write(pickle.dumps({
            "success": False,
            "error_class": "InputDecodeError",
            "error_message": str(exc),
        }))
        return 0

    latex = payload.get("latex", "")
    mode = payload.get("mode", "symbolic")
    precision = int(payload.get("precision", 50))

    # Run the deterministic compute via the existing worker logic.
    from axiom.extensions.builtins.scidisplay import compute_worker

    start = time.monotonic()
    try:
        if mode == "symbolic":
            inner = compute_worker._compute_symbolic(latex)
        elif mode == "numeric":
            inner = compute_worker._compute_numeric(latex)
        elif mode == "arbitrary":
            inner = compute_worker._compute_arbitrary(latex, precision)
        else:
            inner = {
                "success": False,
                "error_class": "UnknownMode",
                "error_message": f"unknown mode {mode!r}",
            }
    except Exception as exc:
        inner = {
            "success": False,
            "error_class": type(exc).__name__,
            "error_message": str(exc),
            "extra": {"traceback": traceback.format_exc()[:500]},
        }
    elapsed_ms = (time.monotonic() - start) * 1000

    if not inner.get("success"):
        sys.stdout.buffer.write(pickle.dumps({
            "success": False,
            "error_class": inner.get("error_class", "Unknown"),
            "error_message": inner.get("error_message", ""),
        }))
        return 0

    # Sign the canonical (latex, mode, value_repr, ast_trail) tuple with
    # this node's Ed25519 keypair. Verifier on the originating side will
    # check the signature against this node's pubkey from the federation
    # directory, never trusting the self-declared pubkey alone.
    from dataclasses import asdict

    from axiom.extensions.builtins.scidisplay.compute_signing import sign_compute_result

    try:
        signed = sign_compute_result(
            latex=latex,
            mode=mode,
            precision=precision,
            value_repr=inner["value_repr"],
            ast_trail=inner["ast_trail"],
            elapsed_ms=elapsed_ms,
        )
    except Exception as exc:
        sys.stdout.buffer.write(pickle.dumps({
            "success": False,
            "error_class": "SigningError",
            "error_message": f"{type(exc).__name__}: {exc}",
        }))
        return 0

    sys.stdout.buffer.write(pickle.dumps({
        "success": True,
        "signed_result": asdict(signed),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
