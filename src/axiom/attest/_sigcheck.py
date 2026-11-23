# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ed25519 checks for a long run of records, spread over a few worker processes (#2200).

Signature verification is nearly all the time a long chain walk takes, and the ``cryptography``
binding holds the GIL while it verifies, so threads do not help; processes do. This module only
answers "does this signature verify over these exact bytes under this exact key" for a list of
``(public_key, message, signature)`` triples. It decides nothing: :func:`axiom.attest.chain.
verify_chain` still walks every record in order, makes every other check itself, and accepts an
answer from here only for the identical triple it would have verified inline. Nothing is kept
between calls, and the workers live only as long as the call.

The workers are ``_sigworker.py`` run as a plain script in an isolated interpreter
(``python -I``): they import ``cryptography`` and nothing else, and never re-run the caller's
``__main__`` the way a ``multiprocessing`` spawn would. Any failure (a worker cannot start,
exits early, answers short or late) gives ``None`` for its triples, and the walk verifies those
inline, exactly as before.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

#: A walk shorter than this is verified inline: starting workers would cost more than it saves.
PARALLEL_MIN = 2_000

#: How long the walk waits for one worker's answers before checking its share inline.
WORKER_TIMEOUT_S = 120.0

_WORKER = Path(__file__).with_name("_sigworker.py")

Triple = tuple[bytes, bytes, bytes]


def max_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def verify_one(public: bytes, message: bytes, signature: bytes, keys: dict[bytes, object]) -> bool:
    """The answer ``keypair.verify`` gave inside ``verify_chain``: ``True`` when the signature
    verifies, ``False`` when it does not or when the key or signature is malformed (``ValueError``).
    Any other error propagates. ``keys`` holds keys already loaded in this call, by exact bytes."""
    cacheable = type(public) is bytes
    key = keys.get(public) if cacheable else None
    try:
        if key is None:
            key = Ed25519PublicKey.from_public_bytes(public)
            if cacheable:
                keys[public] = key
        key.verify(signature, message)  # type: ignore[attr-defined]
        return True
    except InvalidSignature:
        return False
    except ValueError:
        return False


def encode(triples: Sequence[Triple]) -> bytes:
    """A worker's request: a count, then each triple as three length-prefixed byte strings."""
    parts = [struct.pack(">I", len(triples))]
    for t in triples:
        for b in t:
            parts.append(struct.pack(">I", len(b)))
            parts.append(b)
    return b"".join(parts)


class _Worker:
    def __init__(self, indices: list[int], request: bytes) -> None:
        self.indices = indices
        self.answers: dict[int, bool | None] | None = None
        self.proc = subprocess.Popen(  # noqa: S603 - our own script, our own interpreter, no shell
            [sys.executable, "-I", str(_WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._feeder = threading.Thread(target=self._feed, args=(request,), daemon=True)
        self._feeder.start()

    def _feed(self, request: bytes) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(request)
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass

    def collect(self) -> dict[int, bool | None]:
        if self.answers is None:
            out = b""
            try:
                self._feeder.join(timeout=WORKER_TIMEOUT_S)
                assert self.proc.stdout is not None
                out = self.proc.stdout.read()
                self.proc.wait(timeout=WORKER_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - its share is checked inline
                out = b""
            if self.proc.returncode != 0 or len(out) != len(self.indices):
                self.kill()
                out = b""
            self.answers = {
                i: (None if not out else {0: False, 1: True}.get(out[k]))
                for k, i in enumerate(self.indices)
            }
        return self.answers

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001, S110 - best effort; the process is ours and killed
            pass
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass


class Pending:
    """Answers being computed in workers. :meth:`answer` waits only for the worker that holds the
    record asked for, so the walk overlaps with them; ``None`` where no answer is available."""

    def __init__(self, workers: list[_Worker], owner: dict[int, int]) -> None:
        self._workers = workers
        self._owner = owner

    def answer(self, i: int) -> bool | None:
        w = self._owner.get(i)
        return None if w is None else self._workers[w].collect().get(i)

    def close(self) -> None:
        """Stop any worker still running (the walk ended early) and reap them all."""
        for w in self._workers:
            if w.answers is None:
                w.kill()
                w.answers = {}
            elif w.proc.stdout is not None and not w.proc.stdout.closed:
                w.proc.stdout.close()


def submit(triples: Sequence[Triple | None]) -> Pending | None:
    """Start checking the non-``None`` ``triples`` (by index) in worker processes; ``None`` when
    there is nothing to send or no worker could start."""
    todo = [i for i, t in enumerate(triples) if t is not None]
    if not todo:
        return None
    size = -(-len(todo) // max_workers())
    workers: list[_Worker] = []
    owner: dict[int, int] = {}
    for lo in range(0, len(todo), size):
        share = todo[lo : lo + size]
        try:
            worker = _Worker(share, encode([triples[i] for i in share]))  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - this share is checked inline
            continue
        for i in share:
            owner[i] = len(workers)
        workers.append(worker)
    return Pending(workers, owner) if workers else None
