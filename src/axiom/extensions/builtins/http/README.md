# http — Axiom HTTP surface

Two layers live in this extension:

1. The FastAPI app factory + threaded uvicorn runner (`server.py`,
   `__init__.py`) — what consumer extensions mount their routes on.
2. The legacy stdlib chat HTTP API (`chat_server.py`) — what `axi serve`
   runs to expose `ChatAgent` + the OpenAI-compatible endpoints.

This README covers the chat HTTP API surface — the endpoints used to
talk to the chat agent and to benchmark the model in isolation.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/chat` | Send a message, get a response (Axiom-shaped JSON) |
| `POST` | `/v1/chat/completions` | OpenAI-compatible endpoint (RAG-grounded by default) |
| `POST` | `/reset` | Reset the chat session |
| `GET`  | `/v1/info` | Active model identifier + endpoint map |
| `GET`  | `/v1/models` | OpenAI-compatible model list |
| `GET`  | `/health` | Health check |
| `GET`  | `/context` | Available context summary (knowledge sources) |

## Raw bypass — `/v1/chat/completions`

By default, `/v1/chat/completions` runs the full augmented pipeline:
RAG context retrieval, system-prompt injection, tool-use loop, and
session memory. For benchmarking the underlying model in isolation
(comparing wrapped vs. naked output), pass an explicit raw flag:

### Query-parameter form

```bash
curl -X POST 'http://localhost:8766/v1/chat/completions?raw=1' \
  -H 'Content-Type: application/json' \
  -d '{"messages": [{"role": "user", "content": "Hello"}]}'
```

Truthy values: `1`, `true`, `yes`, `on` (case-insensitive).
Anything else (or absent) is treated as `false`.

### Body-field form

```bash
curl -X POST 'http://localhost:8766/v1/chat/completions' \
  -H 'Content-Type: application/json' \
  -d '{
    "messages": [{"role": "user", "content": "Hello"}],
    "raw": true
  }'
```

### Precedence on disagreement

If both forms specify `raw` and disagree, **the body field wins**. The
body is the explicit JSON contract clients post; making it authoritative
keeps client code simpler than reasoning about mixed sources of truth.

### What `raw=true` actually skips

| Layer | Normal | Raw |
|---|---|---|
| System prompt build | identity + policies + CLAUDE.md + RAG block | empty (`""`) |
| Conversation history | full session window | single user turn only |
| Tool surface | full tool list | `None` (no tools exposed) |
| Tool-use loop | up to `MAX_TOOL_ROUNDS` | single gateway call |
| Session mutation | `add_message` for user + assistant + tool | none — fully ephemeral |
| Routing-classifier audit | yes | yes (metadata, not augmentation) |
| Usage tracking | yes | yes |

**`raw=true` is intended for benchmarking, not production.** Production
traffic should leave the flag unset. The benchmark log entries are
tagged `openai-api-raw` in `runtime/logs/chat.jsonl` so post-hoc
analysis can separate raw runs from real traffic.

## `GET /v1/info`

Reports the active model identifier so benchmark runners can record
which model produced each completion without spinning up a `ChatAgent`.

```bash
curl http://localhost:8766/v1/info
```

Response:

```json
{
  "model": "qwen2.5-7b-instruct",
  "raw_supported": true,
  "version": "0.23.1",
  "endpoints": {
    "chat": "/v1/chat/completions",
    "info": "/v1/info",
    "models": "/v1/models"
  }
}
```

The `model` field reads from `axiom.setup.llamafile.DEFAULT_LOCAL_MODEL_ID`.
Switching the bundled default updates this endpoint automatically — no
hardcoded strings.

`version` falls back to `"unknown"` when `importlib.metadata.version("axiom-os-lm")`
raises (some test layouts install the package without a dist record).
The endpoint never fails over a version lookup.

## See also

- `axiom.setup.llamafile.DEFAULT_LOCAL_MODEL_ID` — the bundled local model
  default. The `/v1/info` `model` field reports whatever this resolves to.
- `src/axiom/extensions/builtins/chat/agent.py` — `ChatAgent.turn(raw=...)`
  is the underlying bypass; the HTTP layer just routes to it.
