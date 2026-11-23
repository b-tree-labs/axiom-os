# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A worker for ``_sigcheck``: run as a script (``python -I _sigworker.py``), never imported.

Reads a count and that many ``(public_key, message, signature)`` triples, each part a 4-byte
big-endian length then its bytes, from stdin; writes one byte per triple to stdout: 1 the
signature verifies, 0 it does not or the key or signature is malformed, 2 the check raised
anything else (the caller then checks that one itself). It imports only ``cryptography``.
"""

import struct
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def _read(stream, n):
    data = stream.read(n)
    if len(data) != n:
        raise EOFError("short request")
    return data


def main():
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    (count,) = struct.unpack(">I", _read(inp, 4))
    keys = {}
    answers = bytearray()
    for _ in range(count):
        public, message, signature = (
            _read(inp, struct.unpack(">I", _read(inp, 4))[0]) for _ in range(3)
        )
        try:
            key = keys.get(public)
            if key is None:
                key = keys[public] = Ed25519PublicKey.from_public_bytes(public)
            key.verify(signature, message)
            answers.append(1)
        except InvalidSignature:
            answers.append(0)
        except ValueError:
            answers.append(0)
        except Exception:  # noqa: BLE001 - the caller re-checks this one inline
            answers.append(2)
    out.write(bytes(answers))
    out.flush()


if __name__ == "__main__":
    main()
