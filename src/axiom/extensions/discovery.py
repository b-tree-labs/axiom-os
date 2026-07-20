# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Extension discovery and loading.

Scans extension directories for valid packages with axiom-extension.toml,
loads chat tools, skills, CLI commands, and provider registrations.

Discovery order (highest precedence first):
  1. Project extensions:  .neut/extensions/  (repo-local, found by walking cwd)
  2. User extensions:     ~/<state-dir>/extensions/ (personal, cross-project)
  3. Builtin extensions:  tools/extensions/builtins/ (shipped with package)

Key design choice: uses importlib.util.spec_from_file_location() to load
user extension modules directly from file paths — no pip install, no sys.path
manipulation. Builtin extensions use importlib.import_module() since they are
part of the installed package.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import types
from pathlib import Path
from typing import Any

from axiom.extensions.contracts import (
    MANIFEST_FILENAME,
    Extension,
    Skill,
    parse_manifest,
)
from axiom.infra.paths import get_user_state_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extension directory resolution
# ---------------------------------------------------------------------------


def _builtin_extensions_dir() -> Path:
    """Builtin extensions shipped inside the package.

    Always relative to this file — works in source checkout AND installed wheel.
    """
    return Path(__file__).resolve().parent / "builtins"


def _project_extensions_dir() -> Path | None:
    """Project-local extensions: .neut/extensions/ relative to project root.

    Resolution order:
      1. AXIOM_ROOT env var (explicit override)
      2. Walk up from cwd looking for a .neut/ directory

    Returns None if no project root is found (e.g. clean dir, no .neut/).
    """
    env_root = os.environ.get("AXIOM_ROOT")
    if env_root:
        candidate = Path(env_root).resolve() / ".neut" / "extensions"
        if candidate.is_dir():
            return candidate
        return None

    path = Path.cwd().resolve()
    while path != path.parent:
        candidate = path / ".neut" / "extensions"
        if candidate.is_dir():
            return candidate
        path = path.parent

    return None


def _user_extensions_dir() -> Path:
    """User-level extensions: ~/<state-dir>/extensions/."""
    return get_user_state_dir() / "extensions"


def _norm_pkg(name: str) -> str:
    """Normalize a package/dist name for comparison (PEP 503-ish)."""
    return name.strip().lower().replace("_", "-")


# The platform base is always inherited by every brand (coreutils/systemd are
# never hidden from a distribution). Its own builtins are discovered via the
# builtins dir, not this installed scan, but it's listed here so the predicate
# never hides it when a domain brand is active.
_PLATFORM_BASE_PKG = "axiom-os-lm"


def _is_hidden_sibling(
    dist_name: str, active_pkg: str, member_pkgs: set[str]
) -> bool:
    """ADR-048: should this installed package's extensions be hidden from the
    *active brand*?

    Hidden iff the package is a registered portfolio member (a sibling
    distribution) that is neither the active brand nor the platform base.
    Genuine third-party plugins are NOT portfolio members, so they are never
    hidden — discovery stays universal for the marketplace (Vyzier) case.
    """
    n = _norm_pkg(dist_name)
    members = {_norm_pkg(m) for m in member_pkgs}
    keep = {_norm_pkg(active_pkg), _norm_pkg(_PLATFORM_BASE_PKG)}
    return n in members and n not in keep


def _brand_hidden_packages() -> set[str]:
    """Normalized set of installed-package names to hide under the active brand
    per ADR-048. Empty on any failure (fail open — never wrongly hide)."""
    try:
        from axiom.infra.branding import discover_portfolio_members, get_branding

        active = get_branding().package_name
        members = {m.package_name for m in discover_portfolio_members()}
        return {
            _norm_pkg(m)
            for m in members
            if _is_hidden_sibling(m, active, members)
        }
    except Exception:
        return set()


def _installed_package_extension_dirs() -> list[Path]:
    """Discover extensions from installed packages (e.g., a domain consumer).

    Scans site-packages for packages that have an extensions/builtins/
    directory containing extension manifests. This allows domain packages
    installed via pip to register their extensions with Axiom.

    Convention: any installed package with the structure:
        <package>/extensions/builtins/<ext_name>/neut-extension.toml
    or:
        <package>/extensions/builtins/<ext_name>/axiom-extension.toml
    will be discovered.

    **Performance:** resolves package locations via ``dist.locate_file``
    rather than ``importlib.import_module``. Triggering real imports for
    every installed distribution was the primary CLI-startup bottleneck
    (~4 s with a typical site-packages) because heavy packages like
    numpy or pandas execute meaningful work in their ``__init__``.
    ``locate_file`` only resolves a file path from dist metadata, so
    this function now runs in well under 200 ms on typical venvs.
    """
    dirs: list[Path] = []
    try:
        import concurrent.futures
        import importlib.metadata

        # distributions() can hang on Python 3.14+ due to metaclass
        # overhead. Guard with a timeout so CLI startup is never blocked.
        def _scan_dists():
            return list(importlib.metadata.distributions())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                all_dists = pool.submit(_scan_dists).result(timeout=3)
            except (concurrent.futures.TimeoutError, Exception):
                return dirs

        # Discovery is UNIVERSAL (ADR-048 / AEOS §2.10 "presence, not power"):
        # every installed package's extensions are loaded so they remain
        # invocable. Brand-scoping is a *surfacing* concern applied at listing
        # time via surfaced_extensions(), not here — the .desktop OnlyShowIn
        # model: on PATH everywhere, shown in the menu only under claiming brands.
        for dist in all_dists:
            if dist.name in ("axi-platform", "axiom-os-lm", "axiom-os", "axiom"):
                continue  # Skip ourselves
            try:
                top_level = dist.read_text("top_level.txt")
                if top_level is None:
                    # Infer top-level package from dist name (dist.files is
                    # slow on 3.14+ — it stat()s every file in RECORD).
                    inferred = dist.name.replace("-", "_")
                    top_packages = {inferred} if inferred.isidentifier() else set()
                else:
                    top_packages = {
                        line.strip()
                        for line in top_level.splitlines()
                        if line.strip()
                    }

                for pkg_name in top_packages:
                    # Fast path — resolve via dist metadata (never triggers
                    # module import). Works for normally-installed wheels.
                    pkg_path: Path | None = None
                    try:
                        located = dist.locate_file(pkg_name)
                        if located is not None:
                            candidate = Path(str(located))
                            if candidate.is_dir():
                                pkg_path = candidate
                    except Exception:
                        pkg_path = None

                    # Fallback — editable installs (PEP 660 / legacy
                    # pip-editable) don't live under site-packages, so
                    # locate_file resolves to a non-existent path. Fall
                    # back to importing the module to get its real
                    # __file__. Only fires for packages whose fast path
                    # missed — typically 1–2 editable installs, not the
                    # full site-packages population.
                    if pkg_path is None:
                        try:
                            mod = importlib.import_module(pkg_name)
                            if mod.__file__:
                                pkg_path = Path(mod.__file__).parent
                        except (ImportError, AttributeError):
                            continue

                    if pkg_path is None or not pkg_path.is_dir():
                        continue
                    builtins = pkg_path / "extensions" / "builtins"
                    if not builtins.is_dir():
                        continue
                    # Verify it has at least one extension manifest —
                    # cheap stat-only scan, no TOML parsing.
                    has_manifest = any(
                        (child / MANIFEST_FILENAME).exists()
                        or (child / "neut-extension.toml").exists()
                        for child in builtins.iterdir()
                        if child.is_dir()
                    )
                    if has_manifest:
                        dirs.append(builtins)
            except Exception:
                continue
    except Exception:
        pass
    return dirs


def get_extension_dirs() -> list[Path]:
    """Return extension directories in discovery order.

    Discovery order (highest precedence first):
      1. Project-local:  .neut/extensions/
      2. User-level:     ~/<state-dir>/extensions/
      3. Installed packages: site-packages/<pkg>/extensions/builtins/
      4. Builtins:       axiom/extensions/builtins/

    Earlier entries win when names collide (user can override builtins).
    """
    dirs = []
    project_dir = _project_extensions_dir()
    if project_dir is not None and project_dir.is_dir():
        dirs.append(project_dir)
    user_dir = _user_extensions_dir()
    if user_dir.is_dir():
        dirs.append(user_dir)
    # Installed packages (e.g., a domain consumer)
    dirs.extend(_installed_package_extension_dirs())
    # Axiom builtins (lowest priority)
    builtin_dir = _builtin_extensions_dir()
    if builtin_dir.is_dir():
        dirs.append(builtin_dir)
    return dirs


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_extensions(*search_dirs: Path) -> list[Extension]:
    """Scan extension directories for valid packages.

    Args:
        *search_dirs: Override default search dirs (for testing).
                      If empty, uses get_extension_dirs().

    Returns:
        List of Extension objects, deduplicated by name (first wins).
    """
    dirs = list(search_dirs) if search_dirs else get_extension_dirs()
    seen_names: set[str] = set()
    extensions: list[Extension] = []

    for ext_dir in dirs:
        if not ext_dir.is_dir():
            continue
        for child in sorted(ext_dir.iterdir()):
            if not child.is_dir():
                continue
            # Check both axiom-extension.toml and neut-extension.toml
            manifest = child / MANIFEST_FILENAME
            if not manifest.exists():
                manifest = child / "neut-extension.toml"
            if not manifest.exists():
                continue
            try:
                ext = parse_manifest(manifest)
                if ext.name in seen_names:
                    logger.debug("Skipping duplicate extension: %s", ext.name)
                    continue
                seen_names.add(ext.name)
                extensions.append(ext)
            except Exception as e:
                logger.warning("Failed to parse extension %s: %s", child.name, e)

    return extensions


def _extension_source_package(ext: Extension) -> str | None:
    """Top-level package an installed extension ships in, derived from its path
    (``.../<pkg>/extensions/builtins/<name>``). ``None`` for project-/user-level
    extensions, which live outside any package's ``extensions/builtins`` tree.
    """
    parts = ext.root.parts
    try:
        i = len(parts) - 1 - parts[::-1].index("builtins")
    except ValueError:
        return None
    if i >= 2 and parts[i - 1] == "extensions":
        return parts[i - 2]
    return None


def surfaced_extensions(*search_dirs: Path) -> list[Extension]:
    """The brand-scoped *listing* view of :func:`discover_extensions` (ADR-048).

    Discovery is universal — every installed extension is loaded and remains
    invocable (AEOS §2.10: surfacing governs presence, not power). This view is
    what *listing* surfaces (``ext list``, ``agents status``, menus, completion)
    should iterate: it hides portfolio-sibling extensions that the active brand
    doesn't claim, exactly like a desktop environment honoring ``OnlyShowIn`` —
    the entry stays on PATH, it's just not shown in this menu.

    Resolution paths (running a command, dispatching a heartbeat, loading hooks)
    must keep using :func:`discover_extensions`, never this.
    """
    hidden = _brand_hidden_packages()
    if not hidden:
        return discover_extensions(*search_dirs)
    out: list[Extension] = []
    for ext in discover_extensions(*search_dirs):
        pkg = _extension_source_package(ext)
        if pkg is not None and _norm_pkg(pkg) in hidden:
            continue  # sibling distribution's extension — not shown under this brand
        out.append(ext)
    return out


# ---------------------------------------------------------------------------
# Module loading (importlib, no sys.path)
# ---------------------------------------------------------------------------


def _load_module_from_file(name: str, file_path: Path) -> types.ModuleType:
    """Load a Python module from an arbitrary file path.

    Uses importlib.util.spec_from_file_location — no sys.path manipulation.
    Forces reload on each call to pick up changes immediately (hot-reload).
    """
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Chat tool loading
# ---------------------------------------------------------------------------


def load_chat_tools(ext: Extension) -> list[Any]:
    """Import chat tool modules from an extension.

    Returns list of ToolDef objects from each module's TOOLS list.
    """
    if not ext.chat_tools_module:
        return []

    tools_dir = ext.root / ext.chat_tools_module.replace(".", "/")
    if not tools_dir.is_dir():
        return []

    tool_defs = []
    for py_file in sorted(tools_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        mod_name = f"neut_ext.{ext.name}.{ext.chat_tools_module}.{py_file.stem}"
        try:
            mod = _load_module_from_file(mod_name, py_file)
            for tool_def in getattr(mod, "TOOLS", []):
                tool_defs.append(tool_def)
        except Exception as e:
            logger.warning(
                "Failed to load chat tool %s from %s: %s",
                py_file.name,
                ext.name,
                e,
            )

    return tool_defs


def discover_and_load_chat_tools(*search_dirs: Path) -> list[Any]:
    """Discover all extensions and load their chat tools."""
    tools = []
    for ext in discover_extensions(*search_dirs):
        if ext.enabled:
            tools.extend(load_chat_tools(ext))
    return tools


# ---------------------------------------------------------------------------
# Chat tool execution
# ---------------------------------------------------------------------------


def execute_extension_tool(
    name: str, params: dict[str, Any], *search_dirs: Path
) -> dict[str, Any] | None:
    """Execute an extension chat tool by name.

    Returns the result dict, or None if no extension provides this tool.
    """
    for ext in discover_extensions(*search_dirs):
        if not ext.enabled or not ext.chat_tools_module:
            continue
        tools_dir = ext.root / ext.chat_tools_module.replace(".", "/")
        if not tools_dir.is_dir():
            continue

        for py_file in sorted(tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            mod_name = f"neut_ext.{ext.name}.{ext.chat_tools_module}.{py_file.stem}"
            try:
                mod = _load_module_from_file(mod_name, py_file)
                tool_names = [t.name for t in getattr(mod, "TOOLS", [])]
                if name in tool_names:
                    handler = getattr(mod, "execute", None)
                    if handler:
                        return handler(name, params)
            except Exception as e:
                logger.warning("Error executing %s: %s", name, e)

    return None


# ---------------------------------------------------------------------------
# CLI command discovery
# ---------------------------------------------------------------------------


def discover_cli_commands(*search_dirs: Path) -> dict[str, dict[str, Any]]:
    """Discover CLI commands from all extensions.

    Returns dict mapping noun -> {module, function, description, extension,
    root, builtin, tier, intent_groups, verb_overrides}.

    `tier` / `intent_groups` / `verb_overrides` come from the AEOS 0.1
    manifest schema and drive `axi help` revelation (see `help_engine.py`
    and `prd-axi-cli.md §Progressive Disclosure`). Undeclared commands
    default to `tier="core"`, `intent_groups=[]`, `verb_overrides={}`.
    """
    commands: dict[str, dict[str, Any]] = {}
    for ext in discover_extensions(*search_dirs):
        if not ext.enabled:
            continue
        for cmd in ext.cli_commands:
            if cmd.noun not in commands:
                commands[cmd.noun] = {
                    "module": cmd.module,
                    "function": cmd.function or "main",
                    "description": cmd.description,
                    "extension": ext.name,
                    "root": str(ext.root),
                    "builtin": ext.builtin,
                    "tier": cmd.tier,
                    "intent_groups": list(cmd.intent_groups),
                    "verb_overrides": dict(cmd.verb_overrides),
                    "requires": list(cmd.requires),
                }
    return commands


def discover_connections(*search_dirs: Path) -> list:
    """Discover connection declarations from all extensions (3-tier).

    Returns list of ConnectionDef objects from all enabled extensions.
    Project-local overrides user-global overrides builtins.
    """
    seen_names: set[str] = set()
    connections = []
    for ext in discover_extensions(*search_dirs):
        if not ext.enabled:
            continue
        for conn in ext.connections:
            if conn.name not in seen_names:
                seen_names.add(conn.name)
                connections.append(conn)
    return connections


# ---------------------------------------------------------------------------
# Skill loading
# ---------------------------------------------------------------------------


def load_skills(ext: Extension) -> list[Skill]:
    """Load skills from an extension's skills directory."""
    return ext.skills  # Already scanned during parse_manifest


def discover_all_skills(*search_dirs: Path) -> list[Skill]:
    """Discover all skills from all extensions."""
    skills = []
    for ext in discover_extensions(*search_dirs):
        if ext.enabled:
            skills.extend(ext.skills)
    return skills


# ---------------------------------------------------------------------------
# Contract documentation generation
# ---------------------------------------------------------------------------


def generate_contract_docs() -> str:
    """Generate EXTENSION_CONTRACTS.md content.

    This is the file you paste into Claude/Gemini/Cursor context so they
    know exactly how to build extensions.
    """
    return _CONTRACT_DOCS_TEMPLATE


_CONTRACT_DOCS_TEMPLATE = '''# Extension Contracts

> Auto-generated by `axi ext docs`. Paste this into your AI assistant's
> context so it can generate extensions automatically.

## Quick Start

```bash
axi ext init my-extension    # Scaffold in ~/.neut/extensions/my-extension/
axi ext                      # List installed extensions
axi ext check my-extension   # Validate
```

## Manifest: `axiom-extension.toml`

Every extension has a `axiom-extension.toml` at its root:

```toml
[extension]
name = "my-extension"
version = "0.1.0"
description = "What this extension does"
author = "Your Name"

# Chat tools — Python modules with TOOLS list + execute() function
[chat_tools]
module = "tools_ext"

# Skills — SKILL.md standard (compatible with Claude Code, Codex, Copilot)
[skills]
dir = "skills"

# CLI commands — new nouns for the axi CLI
[[cli.commands]]
noun = "myverb"
module = "cli.myverb"
description = "Do something custom"

# Docflow providers
[[providers]]
type = "generation"
name = "pptx"
module = "providers.pptx_generation"

# Sense extractors
[[extractors]]
name = "data_log"
module = "extractors.data_log"
file_patterns = ["*.log", "*.csv"]

# MCP servers (same schema as .mcp.json)
[mcp_servers.my_server]
type = "stdio"
command = "python"
args = ["-m", "my_mcp_server"]
env = { API_KEY = "${MY_API_KEY}" }

# External connections (APIs, CLI tools, browser sessions)
[[connections]]
name = "my_service"
display_name = "My Service"
kind = "api"                              # "api" | "browser" | "mcp" | "a2a" | "cli"
category = "data"                         # Logical grouping for UI
credential_env_var = "MY_SERVICE_TOKEN"   # Primary credential source
docs_url = "https://example.com/api-keys" # Where to get credentials

# Optional: health checking
health_check = "http_get"                 # "http_get" | "tcp_connect" | "cli_version" | "custom"
health_endpoint = "https://api.example.com/health"

# Optional: declarative installation
[connections.install_commands]
macos = "brew install my-service"
linux = "sudo apt-get install -y my-service"

# Optional: lifecycle hooks (for services that need to be running)
# ensure_module = "my_extension.connections"
# ensure_function = "ensure_my_service_running"  # () -> bool, silent, no prompts
# post_setup_module = "my_extension.connections"
# post_setup_function = "setup_my_service"        # () -> int, interactive, runs once
```

---

## 1. Chat Tool Contract

Each `.py` file in the `tools_ext/` directory exports:
- `TOOLS`: list of `ToolDef` objects
- `execute(name: str, params: dict) -> dict`: handler function

```python
from axiom.extensions.builtins.chat.tools import ToolDef
from axiom.infra.orchestrator.actions import ActionCategory

TOOLS = [
    ToolDef(
        name="my_tool",
        description="What this tool does (shown to the LLM).",
        category=ActionCategory.READ,  # READ = auto-approved, WRITE = needs confirmation
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
            },
            "required": ["query"],
        },
    ),
]

def execute(name: str, params: dict) -> dict:
    """Execute tool. Always return a dict."""
    if name == "my_tool":
        query = params.get("query", "")
        return {"results": [f"Result for: {query}"], "count": 1}
    return {"error": f"Unknown tool: {name}"}
```

**Parameter schema** follows OpenAI function-calling format (JSON Schema subset):
- `type`: "string", "number", "boolean", "integer", "array", "object"
- `description`: Shown to the LLM
- `enum`: Optional restricted values
- `required`: List of required parameter names

**ActionCategory:**
- `READ` — Auto-approved, no user confirmation needed
- `WRITE` — Requires human confirmation before execution

---

## 2. Skill Contract (SKILL.md)

Skills use the Agent Skills standard (compatible with Claude Code, Codex, Copilot).
Each skill lives in its own directory under `skills/`:

```
skills/
    weekly-slides/
        SKILL.md
    data-export/
        SKILL.md
```

SKILL.md format:

```markdown
---
name: weekly-slides
description: Generate weekly progress slides from sense data
---

# Weekly Slides

## Instructions

1. Query sense status for the current week's signals
2. Group signals by initiative
3. Generate a slide deck with one slide per initiative
4. Include blockers slide at the end

## Parameters

- **format**: Output format (pptx, pdf). Default: pptx
- **week**: ISO date for the week. Default: current week
```

---

## 3. CLI Command Contract

CLI command modules export a `main()` function and optionally `get_parser()`:

```python
import argparse

def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi myverb",
        description="Do something custom",
    )
    parser.add_argument("target", help="What to operate on")
    parser.add_argument("--format", choices=["json", "table"], default="table")
    return parser

def main():
    parser = get_parser()
    args = parser.parse_args()
    print(f"Operating on {args.target} with format {args.format}")
```

---

## 4. Docflow Provider Contracts

Providers implement abstract base classes from `tools.extensions.builtins.publishing.providers.base`:

### GenerationProvider

```python
from pathlib import Path
from axiom.extensions.builtins.publishing.providers.base import (
    GenerationProvider,
    GenerationOptions,
    GenerationResult,
)

class PptxGenerationProvider(GenerationProvider):
    def __init__(self, config: dict):
        self.config = config

    def generate(self, source_path: Path, output_path: Path,
                 options: GenerationOptions) -> GenerationResult:
        # Convert markdown to .pptx
        # Return GenerationResult with output_path, format, size_bytes
        ...

    def rewrite_links(self, artifact_path: Path, link_map: dict[str, str]) -> None:
        pass  # Optional for pptx

    def get_output_extension(self) -> str:
        return ".pptx"

    def supports_watermark(self) -> bool:
        return False
```

### StorageProvider, NotificationProvider

See `tools/publisher/providers/base.py` for all five provider ABCs.

---

## 5. Extractor Contract

Extractors inherit from `tools.extensions.builtins.signals.extractors.base.BaseExtractor`:

```python
from pathlib import Path
from axiom.extensions.builtins.signals.extractors.base import BaseExtractor
from axiom.extensions.builtins.signals.models import Extraction, Signal

class DataLogExtractor(BaseExtractor):
    @property
    def name(self) -> str:
        return "data_log"

    def can_handle(self, path: Path) -> bool:
        return path.exists() and path.suffix in (".log", ".csv")

    def extract(self, source: Path, **kwargs) -> Extraction:
        # Parse log file, create Signal objects
        signals = []
        # ... extraction logic ...
        return Extraction(
            extractor=self.name,
            source_file=str(source),
            signals=signals,
        )
```

---

## 6. MCP Server Contract

Same JSON schema as `.mcp.json` (Claude Code, Cursor):

```toml
[mcp_servers.my_server]
type = "stdio"
command = "python"
args = ["-m", "my_mcp_module"]
env = { API_KEY = "${MY_API_KEY}" }
```

Supports `${VAR}` and `${VAR:-default}` expansion for credentials.

---

## 7. Connection Contract

Connections declare external dependencies. The platform handles credential
resolution, health checks, installation, and service lifecycle.

### Declaration (TOML)

```toml
[[connections]]
name = "redis"                            # Unique identifier
display_name = "Redis"                    # Shown in `axi connect` and `axi status`
kind = "cli"                              # Type: "api" | "browser" | "mcp" | "a2a" | "cli"
category = "data"                         # Grouping: "llm", "code", "data", "storage", "tools", etc.
endpoint = "redis-server"                 # URL, host:port, or binary name (for CLI tools)
credential_type = "none"                  # "api_key" | "none" | "browser_session" | "oauth_token"
credential_env_var = ""                   # Env var for credential (e.g., "REDIS_TOKEN")
required = false                          # If true, axi warns on missing
health_check = "tcp_connect"              # How to verify: "http_get", "tcp_connect", "cli_version", "custom"
health_endpoint = "localhost:6379"        # Target for health check (if different from endpoint)
docs_url = "https://redis.io/docs"        # Where users get credentials or install info

# Capabilities (read/write/admin)
capabilities = ["read", "write"]          # What this connection supports

[connections.install_commands]             # Platform-specific install commands
macos = "brew install redis"
linux = "sudo apt-get install -y redis-server"

# Lifecycle hooks — keep tool-specific logic in YOUR extension, not the platform
# ensure_module/ensure_function: called silently before first use (auto-start)
# post_setup_module/post_setup_function: called interactively by `axi connect`
```

### Usage (Python)

```python
from axiom.infra.connections import get_credential, get_cli_tool, ensure_available

# API connections — get the credential (returns None if missing, never throws)
token = get_credential("my_service")
if token is None:
    return []  # Graceful degradation

# CLI tools — resolve binary path and version
tool = get_cli_tool("redis")
if tool:
    subprocess.run([tool.path, "ping"])

# Services — ensure running before use (calls declared ensure hook)
if ensure_available("redis"):
    # Redis is guaranteed to be serving
    ...
```

### Rules for Extension Builders

1. **Never hardcode credentials** — use `get_credential()`
2. **Never store credentials in runtime/** — platform handles `~/.neut/credentials/`
3. **Always degrade gracefully** — `get_credential()` returns `None`, handle it
4. **Declare connections in the manifest** — platform discovers and health-checks them
5. **Provide `docs_url`** — tells users where to get credentials
6. **Keep lifecycle logic in your extension** — use `ensure_module`, not platform code
7. **Use `install_commands`** — let the platform handle installation prompts

---

## 8. Persistence Guidelines

**Default: file-based** (JSON/TOML in extension directory)
- Human-readable, easy backup, git-friendly
- Good for: config, session state, audit trails

**PostgreSQL** (when needed):
- Growing data, vector search, relational queries, shared state
- Declare in manifest: `[database]` section with migrations dir

---

## Directory Structure

```
~/.neut/extensions/my-extension/
    axiom-extension.toml     # Manifest (required)
    tools_ext/              # Chat tools (Python modules)
        my_tool.py
    skills/                 # Agent skills (SKILL.md standard)
        weekly-slides/
            SKILL.md
    cli/                    # CLI commands
        myverb.py
    providers/              # Docflow providers
        pptx_generation.py
    extractors/             # Sense extractors
        data_log.py
```

Extensions are hot-reloaded — no pip install, no restart needed.
'''
