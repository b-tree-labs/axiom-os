# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""HTTP API server for the Axiom chat agent.

Zero external dependencies — uses only stdlib http.server.
Exposes ChatAgent over HTTP with CORS, so any web page can embed a live agent.

Endpoints:
    POST /chat                         Send a message, get a response
    POST /v1/chat/completions          OpenAI-compatible endpoint (RAG-grounded
                                       by default; pass ``?raw=1`` or
                                       ``{"raw": true}`` to bypass the wrapper
                                       for benchmarking)
    GET  /v1/info                      Active model identifier + endpoint map
                                       (used by benchmark runners to record
                                       which model produced each completion)
    GET  /v1/models                    OpenAI-compatible model list
    GET  /health                       Health check
    GET  /context                      Available context summary
    OPTIONS /*                         CORS preflight

Usage:
    axi serve [--port 8766] [--host 0.0.0.0] [--origins "*"]

Security:
    - Optional API key via --api-key or AXIOM_API_KEY env var
    - CORS origin allowlist via --origins (default: localhost only)
    - Read-only tool execution (write tools require explicit opt-in)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

from axiom.infra.state import locked_append_jsonl

logger = logging.getLogger(__name__)

# Lazy-loaded to avoid import overhead at module level
_agent = None
_agent_lock = threading.Lock()
_chat_log_path: Path | None = None
_chat_log_lock = threading.Lock()


def _log_chat(user: str, message: str, response: str, elapsed_ms: int):
    """Append a chat exchange to the JSONL log file."""
    if not _chat_log_path:
        return
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "user": user,
        "prompt": message,
        "response": response,
        "elapsed_ms": elapsed_ms,
    }
    with _chat_log_lock:
        locked_append_jsonl(_chat_log_path, entry)


_RAW_TRUTHY = {"1", "true", "yes", "on"}
_RAW_FALSY = {"0", "false", "no", "off", ""}


def _parse_raw_query(path: str) -> bool | None:
    """Parse the ``raw`` query parameter from *path*.

    Returns ``True`` / ``False`` if a recognized value was present, or
    ``None`` if the parameter was absent. Recognized truthy values:
    ``1, true, yes, on`` (case-insensitive). Anything else is treated
    as ``False``.
    """
    query = urlsplit(path).query
    if not query:
        return None
    params = parse_qs(query, keep_blank_values=True)
    if "raw" not in params:
        return None
    val = (params["raw"][0] or "").strip().lower()
    if val in _RAW_TRUTHY:
        return True
    if val in _RAW_FALSY:
        return False
    return False


def _resolve_axiom_version() -> str:
    """Best-effort lookup of the installed axi-platform version.

    Falls back to ``"unknown"`` when the metadata is unavailable (some
    test environments install the package as a source layout without a
    real dist). Never raises — the ``/v1/info`` endpoint must not fail
    over a version lookup.
    """
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("axi-platform")
    except Exception:
        logger.debug("axi-platform version lookup failed", exc_info=True)
        return "unknown"


_rag_store = None
_rag_store_lock = threading.Lock()


def _get_rag_store():
    """Lazy-init RAG store for federation search."""
    global _rag_store
    if _rag_store is not None:
        return _rag_store
    with _rag_store_lock:
        if _rag_store is not None:
            return _rag_store
        import os

        url = os.environ.get("DATABASE_URL", "")
        if not url:
            try:
                from axiom.setup.secrets import get_secret

                pg_pass = get_secret("AXIOM_PG_PASSWORD")
                if pg_pass:
                    url = f"postgresql://axiom:{pg_pass}@localhost:5432/axiom_db"
            except Exception:
                pass
        if not url:
            raise RuntimeError("No DATABASE_URL configured")
        from axiom.rag.store import RAGStore

        store = RAGStore(url)
        store.connect()
        _rag_store = store
        return _rag_store


def _get_agent():
    """Lazy-init ChatAgent with gateway and session."""
    global _agent
    if _agent is not None:
        return _agent

    with _agent_lock:
        if _agent is not None:
            return _agent

        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.bus import EventBus
        from axiom.infra.gateway import Gateway
        from axiom.infra.orchestrator.session import Session

        gateway = Gateway()
        bus = EventBus()
        session = Session()

        _agent = ChatAgent(gateway=gateway, bus=bus, session=session)
        logger.info(
            "ChatAgent initialized (provider: %s, model: %s)",
            gateway.active_provider.name if gateway.active_provider else "stub",
            gateway.active_provider.model if gateway.active_provider else "none",
        )
        return _agent


def _get_context_summary() -> dict:
    """Return a summary of what institutional knowledge the agent has access to."""
    from axiom import REPO_ROOT  # pylint: disable=import-outside-toplevel
    from axiom.infra.branding import get_branding  # pylint: disable=import-outside-toplevel

    context = {
        "project": get_branding().product_name,
        "description": "Modular digital platform for facilities",
        "knowledge_sources": [],
    }

    # Check for CLAUDE.md
    claude_md = REPO_ROOT / "CLAUDE.md"
    if claude_md.exists():
        context["knowledge_sources"].append(
            {
                "type": "project_context",
                "name": "CLAUDE.md",
                "description": "Project conventions, architecture, and institutional knowledge",
            }
        )

    # Check for docs
    docs_dir = REPO_ROOT / "docs"
    if docs_dir.exists():
        doc_count = sum(1 for _ in docs_dir.rglob("*.md"))
        context["knowledge_sources"].append(
            {
                "type": "documentation",
                "name": "docs/",
                "count": doc_count,
                "description": "PRDs, tech specs, analysis, stakeholder inputs",
            }
        )

    # Check for runtime/config
    runtime_dir = REPO_ROOT / "runtime"
    if runtime_dir.exists():
        context["knowledge_sources"].append(
            {
                "type": "runtime_config",
                "name": "runtime/",
                "description": "Model configuration, facility settings",
            }
        )

    # Check sense inbox
    inbox = REPO_ROOT / "runtime" / "inbox" / "processed"
    if inbox.exists():
        processed = sum(1 for _ in inbox.rglob("*") if _.is_file())
        context["knowledge_sources"].append(
            {
                "type": "signals",
                "name": "sense inbox",
                "count": processed,
                "description": "Processed signals from GitLab, Teams, meetings",
            }
        )

    return context


class NeutAPIHandler(BaseHTTPRequestHandler):
    """HTTP handler for the Axiom chat API."""

    # Set by the server
    allowed_origins: list[str] = ["http://localhost:*"]
    api_key: str | None = None
    read_only: bool = True
    static_dir: str | None = None  # Path to serve static files from

    def log_message(self, format, *args):
        logger.info(format, *args)

    def _set_cors_headers(self):
        origin = self.headers.get("Origin", "")
        if self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
        elif "*" in self.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")

    def _origin_allowed(self, origin: str) -> bool:
        if not origin:
            return False
        if "*" in self.allowed_origins:
            return True
        parsed = urlparse(origin)
        for allowed in self.allowed_origins:
            if "*" in allowed:
                # Simple wildcard: http://localhost:* matches any port
                pattern = allowed.replace("*", "")
                if origin.startswith(pattern) or (parsed.hostname and parsed.hostname in allowed):
                    return True
            elif origin == allowed:
                return True
        return False

    def _check_auth(self) -> bool:
        if not self.api_key:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:] == self.api_key
        return False

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self):
        # Strip query string for routing decisions.
        route = self.path.split("?", 1)[0]
        if route == "/health":
            self._send_json(200, {"status": "ok", "service": "neut-api"})
        elif route == "/v1/models":
            self._handle_openai_models()
        elif route == "/v1/info":
            self._handle_info()
        elif route == "/context":
            self._send_json(200, _get_context_summary())
        elif self.static_dir:
            self._serve_static()
        else:
            self._send_json(404, {"error": "Not found"})

    def _serve_static(self):
        """Serve static files from the configured directory."""
        import mimetypes
        from pathlib import Path

        # Map / to /index.html
        req_path = self.path.split("?")[0]
        if req_path == "/":
            req_path = "/index.html"

        # Resolve and prevent directory traversal
        assert self.static_dir is not None
        static_root = Path(self.static_dir).resolve()
        file_path = (static_root / req_path.lstrip("/")).resolve()
        if not str(file_path).startswith(str(static_root)):
            self._send_json(403, {"error": "Forbidden"})
            return

        if not file_path.is_file():
            self._send_json(404, {"error": "Not found"})
            return

        content_type, _ = mimetypes.guess_type(str(file_path))
        content_type = content_type or "application/octet-stream"
        body = file_path.read_bytes()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Strip query string for routing decisions; specific handlers
        # parse query params themselves (e.g. ``?raw=1``).
        route = self.path.split("?", 1)[0]

        # Federation endpoint uses its own auth (Ed25519 signatures),
        # not the server's API key — check it before the API key gate.
        if route == "/api/v1/rag/search":
            self._handle_federation_search()
            return

        if not self._check_auth():
            self._send_json(401, {"error": "Unauthorized"})
            return

        if route == "/chat":
            self._handle_chat()
        elif route == "/v1/chat/completions":
            self._handle_openai_chat()
        elif route == "/reset":
            self._handle_reset()
        else:
            self._send_json(404, {"error": "Not found"})

    def _handle_chat(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "Invalid JSON"})
            return

        message = data.get("message", "").strip()
        if not message:
            self._send_json(400, {"error": "Empty message"})
            return

        if len(message) > 4000:
            self._send_json(400, {"error": "Message too long (max 4000 chars)"})
            return

        # Optional user context from client (EID-based identity)
        user_context = data.get("user_context")

        try:
            agent = _get_agent()

            # Inject user identity into session context if provided
            if user_context and isinstance(user_context, dict):
                agent.session.context["user_identity"] = user_context

            t0 = time.monotonic()
            response = agent.turn(message, stream=False)
            elapsed_ms = round((time.monotonic() - t0) * 1000)

            user_name = (user_context or {}).get("name", "anonymous")
            _log_chat(user_name, message, response, elapsed_ms)

            self._send_json(
                200,
                {
                    "response": response,
                    "elapsed_ms": elapsed_ms,
                },
            )
        except Exception as e:
            logger.error("Chat error: %s", traceback.format_exc())
            self._send_json(500, {"error": str(e)})

    def _handle_reset(self):
        """Reset the chat session."""
        global _agent
        with _agent_lock:
            _agent = None
        self._send_json(200, {"status": "session reset"})

    def _handle_openai_chat(self):
        """OpenAI-compatible /v1/chat/completions endpoint.

        Drop-in replacement for direct LLM access. Users change only
        the base URL — same API key, same request format, same response
        format — but get RAG-grounded answers automatically.

        Migration: change ``base_url`` from Qwen's port to axi serve's port.

        Raw bypass (raw-model benchmark support): pass ``?raw=1`` in
        the query string OR ``"raw": true`` in the JSON body to skip the
        RAG / system-prompt / tool-routing wrapper and forward the user
        message straight to the model. Intended for benchmarking only —
        production traffic should NOT set the flag. If both query and body
        specify ``raw`` and disagree, the body field wins (it's the
        explicit JSON contract).
        """
        # Parse the raw flag from the query string before consuming the
        # body — query parsing is platform-independent (stdlib only).
        query_raw = _parse_raw_query(self.path)

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_json(
                400, {"error": {"message": "Invalid JSON", "type": "invalid_request_error"}}
            )
            return

        messages = data.get("messages", [])
        if not messages:
            self._send_json(
                400, {"error": {"message": "messages is required", "type": "invalid_request_error"}}
            )
            return

        # Body field wins over query param on disagreement.
        body_raw = data.get("raw")
        if isinstance(body_raw, bool):
            raw_mode = body_raw
        elif query_raw is not None:
            raw_mode = query_raw
        else:
            raw_mode = False

        # Extract the last user message
        user_msg = ""
        for m in messages:
            if m.get("role") == "user":
                user_msg = m.get("content", "")
            elif m.get("role") == "system":
                pass  # System messages handled by agent's built-in system prompt

        if not user_msg:
            self._send_json(
                400,
                {"error": {"message": "No user message found", "type": "invalid_request_error"}},
            )
            return

        try:
            agent = _get_agent()
            t0 = time.monotonic()

            # raw_mode=True bypasses RAG, system prompt, and tool routing.
            # raw_mode=False (the default) keeps the full augmented pipeline.
            response_text = agent.turn(user_msg, stream=False, raw=raw_mode)
            elapsed_ms = round((time.monotonic() - t0) * 1000)

            log_user = "openai-api-raw" if raw_mode else "openai-api"
            _log_chat(log_user, user_msg, response_text, elapsed_ms)

            # Return OpenAI-compatible response
            import uuid

            self._send_json(
                200,
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": data.get("model", "axiom-rag"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": response_text,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": len(user_msg.split()),
                        "completion_tokens": len(response_text.split()),
                        "total_tokens": len(user_msg.split()) + len(response_text.split()),
                    },
                },
            )
        except Exception as e:
            logger.error("OpenAI chat error: %s", traceback.format_exc())
            self._send_json(500, {"error": {"message": str(e), "type": "server_error"}})

    def _handle_info(self):
        """GET /v1/info — report the active model identifier + endpoint map.

        Used by benchmark runners to record which model
        produced each completion. Cheap (no agent spin-up): reads the
        model id straight from :mod:`axiom.setup.llamafile` and the
        package version from ``importlib.metadata`` with a soft fallback
        to ``"unknown"`` so that a missing dist record never breaks the
        endpoint.
        """
        from axiom.setup.llamafile import DEFAULT_LOCAL_MODEL_ID

        self._send_json(
            200,
            {
                "model": DEFAULT_LOCAL_MODEL_ID,
                "raw_supported": True,
                "version": _resolve_axiom_version(),
                "endpoints": {
                    "chat": "/v1/chat/completions",
                    "info": "/v1/info",
                    "models": "/v1/models",
                },
            },
        )

    def _handle_openai_models(self):
        """OpenAI-compatible /v1/models endpoint.

        Returns the available models. Clients that call this to discover
        models will see 'axiom-rag' — indicating this is a RAG-enhanced endpoint.
        """
        try:
            agent = _get_agent()
            backend_model = ""
            if agent.gateway.providers:
                p = agent.gateway.providers[0]
                backend_model = p.model
        except Exception:
            backend_model = "unknown"

        self._send_json(
            200,
            {
                "object": "list",
                "data": [
                    {
                        "id": "axiom-rag",
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "axiom",
                        "description": f"RAG-grounded responses via {backend_model}. "
                        "Same as direct LLM but with knowledge base context injection.",
                    }
                ],
            },
        )

    def _handle_federation_search(self):
        """Handle POST /api/v1/rag/search from federation peers.

        Auth is handled by the federation endpoint module — NOT by
        the server's API key (peers use Ed25519 signatures, not shared keys).
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
        except Exception:
            self._send_json(400, {"error": "Could not read body"})
            return

        try:
            from axiom.extensions.builtins.http.federation_endpoint import (
                handle_federation_search,
            )
        except ImportError:
            self._send_json(501, {"error": "Federation endpoint not available"})
            return

        # Build a request-like object for the handler
        class _Req:
            pass

        req = _Req()
        req.headers = dict(self.headers)
        req.body = body

        # Get the RAG store
        try:
            store = _get_rag_store()
        except Exception:
            self._send_json(503, {"error": "RAG store not available"})
            return

        status, response_body = handle_federation_search(req, store=store)
        self._send_json(status, json.loads(response_body))


class NeutAPIServer:
    """Configurable HTTP API server for the Axiom chat agent."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8766,
        origins: list[str] | None = None,
        api_key: str | None = None,
        read_only: bool = True,
        static_dir: str | None = None,
    ):
        self.host = host
        self.port = port
        self.origins = origins or ["http://localhost:*", "http://127.0.0.1:*"]
        self.api_key = api_key or os.environ.get("AXIOM_API_KEY")
        self.read_only = read_only
        self.static_dir = static_dir

    def serve(self):
        # Configure handler class attributes
        NeutAPIHandler.allowed_origins = self.origins
        NeutAPIHandler.api_key = self.api_key
        NeutAPIHandler.read_only = self.read_only
        NeutAPIHandler.static_dir = self.static_dir

        # Set up chat log
        global _chat_log_path
        from axiom import REPO_ROOT

        log_dir = REPO_ROOT / "runtime" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _chat_log_path = log_dir / "chat.jsonl"

        server = HTTPServer((self.host, self.port), NeutAPIHandler)

        auth_status = "enabled" if self.api_key else "disabled"
        origins_str = ", ".join(self.origins)

        print("Axiom API server")
        print(f"  Listening:  http://{self.host}:{self.port}")
        print(f"  Auth:       {auth_status}")
        print(f"  CORS:       {origins_str}")
        print(f"  Read-only:  {self.read_only}")
        print()
        print("Endpoints:")
        print("  POST /chat     Send a message")
        print("  POST /reset    Reset session")
        print("  GET  /health   Health check")
        print("  GET  /context  Knowledge sources")
        print()

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")
            server.shutdown()
