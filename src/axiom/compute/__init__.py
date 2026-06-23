# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axiom.compute — Phase 0 atomic dispatch primitives for the twin toolkit.

Public API:

- DispatchSpec — the input contract for a kernel run
- DispatchResult — successful run, signed receipt
- HaltedDispatchResult — auto-stopped run, signed halted-receipt
- HaltCondition — the watch condition that fired
- dispatch(spec) — execute the spec, return a result
- verify_signature(result) — verify the receipt's signature

Per ADR-016: this is the library layer; no `axi compute` CLI exposure.
A domain consumer wraps these primitives (e.g. as `neut model run`) for the user surface.

Per ADR-018 (revised 2026-05-04): the mock kernel ships as plain Python
(no WASM); same for validators, parsers, watch evaluators by default.
"""

from axiom.compute.dispatch import (
    DispatchSpec,
    DispatchResult,
    HaltedDispatchResult,
    HaltCondition,
    dispatch,
    verify_signature,
)

__all__ = [
    "DispatchSpec",
    "DispatchResult",
    "HaltedDispatchResult",
    "HaltCondition",
    "dispatch",
    "verify_signature",
]
