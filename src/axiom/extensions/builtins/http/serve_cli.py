# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi serve` — HTTP API server for axi chat.

Usage:
    axi serve                          Start on port 8766
    axi serve --port 9000              Custom port
    axi serve --origins "*"            Allow all CORS origins
    axi serve --api-key SECRET         Require auth
"""

from __future__ import annotations

import argparse


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi serve",
        description="Start the axi HTTP API server",
    )
    parser.add_argument(
        "--port", type=int, default=8766,
        help="Port to listen on (default: 8766)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1 / loopback; "
             "pass 0.0.0.0 to expose on all interfaces)",
    )
    parser.add_argument(
        "--origins", nargs="*", default=None,
        help='Allowed CORS origins (default: localhost only, use "*" for all)',
    )
    parser.add_argument(
        "--api-key", default=None,
        help="API key for auth (or set AXIOM_API_KEY env var)",
    )
    parser.add_argument(
        "--read-only", action="store_true", default=True,
        help="Only allow read-only tools (default: true)",
    )
    parser.add_argument(
        "--static-dir", default=None,
        help="Directory to serve static files from (served at /)",
    )
    return parser


def main():
    parser = get_parser()
    args = parser.parse_args()

    from .chat_server import NeutAPIServer

    server = NeutAPIServer(
        host=args.host,
        port=args.port,
        origins=args.origins,
        api_key=args.api_key,
        read_only=args.read_only,
        static_dir=args.static_dir,
    )
    server.serve()


if __name__ == "__main__":
    main()
