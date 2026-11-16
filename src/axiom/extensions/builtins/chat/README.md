<!-- Copyright (c) 2026 The University of Texas at Austin -->
<!-- Copyright (c) 2026 B-Tree Labs -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# chat

**AXI, the loop agent, and the tool registry the loop calls.**

A person talks to AXI. AXI calls tools. A write waits for approval. A read runs. The same turn can resume, stream, and carry session memory. The CLI noun is `chat`. The agent name is `axi`. A consumer distribution may show another face. The agent identity stays AXI.

Conformance is [AEOS 0.1](../../../../../docs/specs/spec-aeos-0.1.md) with the 0.2 draft chain amendments. The manifest sets `aeos_version = "0.2.0"` and `chain_level = "B"`. Chat declares no `kind = "tool"`. It consumes tools. Its read-only command forms are skills, projected to MCP as `axiom_chat__<verb>`.

## Where it sits

```mermaid
flowchart TD
  PERSON["Person"]:::core
  CLI["commands/cli.py<br/>axi chat"]:::neutral
  API["api.py<br/>/api/v1/chat<br/>webapp registry"]:::neutral
  AXI["agents/axi<br/>ChatAgent"]:::accent
  LIB["library.py<br/>Python callers"]:::data
  TOOLS["tools/<br/>ToolDef registry"]:::data
  GATE["Approval gate<br/>writes wait, reads run"]:::vega
  SK["skills/<br/>chat.tools chat.stats chat.where<br/>chat.memory_search and the rest"]:::accent
  MCP["mcp extension<br/>axiom_chat__ verb"]:::core
  MEM["memory extension<br/>when the turn asks"]:::vega

  PERSON --> CLI
  PERSON --> API
  CLI --> AXI
  API --> AXI
  AXI --> TOOLS
  LIB --> TOOLS
  TOOLS --> GATE
  TOOLS --> SK
  SK --> MCP
  AXI --> MEM
```

`/api/v1/chat` is chat's slice of the composed API. The webapp registry mounts it. webapp does not import chat.

## Pieces

```mermaid
flowchart TD
  MAN["axiom-extension.toml<br/>agent cmd api skill<br/>chain level B"]:::neutral
  CMD["commands/cli.py<br/>REPL"]:::core
  SLASH["commands/__init__.py<br/>slash commands"]:::core
  AGT["agent.py<br/>ChatAgent"]:::accent
  REG["tools/__init__.py<br/>get_all_tools execute_tool"]:::data
  EXT["tools_ext/<br/>hot-loaded extension tools"]:::data
  READ["skills/read.py<br/>read-only skills"]:::accent
  LIB["library.py<br/>ToolDef get_all_tools<br/>execute_tool get_tool_definitions<br/>unknown_tool_message"]:::data

  MAN --> CMD
  MAN --> AGT
  MAN --> READ
  CMD --> AGT
  CMD --> SLASH
  AGT --> REG
  REG --> EXT
  REG --> READ
  LIB --> REG
```

| Piece | What it is |
|---|---|
| `commands/cli.py` | `axi chat`. Starts, resumes, and runs the REPL. |
| `commands/__init__.py` | Slash commands the REPL dispatches. |
| `agent.py` | `ChatAgent`. The turn loop, tool calls, approval, streaming, session. |
| `tools/` | The toolkit. `ToolDef` records, `get_all_tools`, `execute_tool`. Extension tools load from `tools_ext/`. |
| `library.py` | The Python library for that registry. |
| `skills/read.py` | Read-only skills. Chain level B: each declares risk, and a refusal carries a fix. |
| `api.py` | Conversation routes under `/api/v1/chat`. |

`cli.py` at the extension root imports `commands/cli.py`.

## One turn

```mermaid
flowchart TD
  IN["User text"]:::core
  AXI["ChatAgent"]:::accent
  LIST["get_all_tools<br/>skills, manifest tools, tools_ext"]:::data
  CALL["Model tool call"]:::neutral
  KIND{"Write or read"}:::vega
  WAIT["Approval<br/>person says yes or no"]:::vega
  RUN["execute_tool"]:::accent
  OUT["Result back into the turn<br/>unknown name gets unknown_tool_message"]:::data

  IN --> AXI
  AXI --> LIST
  LIST --> CALL
  CALL --> KIND
  KIND -->|write| WAIT
  KIND -->|read| RUN
  WAIT -->|yes| RUN
  WAIT -->|no| OUT
  RUN --> OUT
```

`chat.memory_search` and `chat.memory_recent` produce a `fragment_id`. The next call is `chat.memory_show` with that id. The chain declarations live on the skill blocks in the manifest.

## Toolkit and library

```python
from axiom.extensions.builtins.chat import execute_tool, get_all_tools
```

Those names resolve through `library.py` on first use. Importing the package does not build the registry. `get_tool_definitions` is the list the model sees. `unknown_tool_message` is the text returned for a name that was not offered.
