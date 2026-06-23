# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Extension manifest model and data structures.

Pure data — no I/O, no imports of external modules. Defines the shape of
extension manifests (axiom-extension.toml) and the types that discovery and
scaffold modules operate on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CLICommandDef:
    """A CLI noun registered by an extension.

    `entry` is the AEOS-canonical entry-point string (`"module.path:funcname"`).
    `module` is retained as a convenience field — the module portion of `entry`
    — so consumers that want just the module path don't have to parse.
    `function` is the symbol name to call after import; defaults to `"main"`.

    `tier` / `intent_groups` / `verb_overrides` come from the AEOS 0.1
    manifest schema; they drive incremental revelation in `axi help`
    (per `prd-axi-cli.md §Progressive Disclosure`). The defaults below
    keep undeclared commands behaving as they always have — `core` is
    visible to every user from `core` upward (the day-to-day surface).
    """

    noun: str
    module: str  # Dotted module path
    description: str = ""
    function: str = "main"
    entry: str = ""
    tier: str = "core"
    intent_groups: list[str] = field(default_factory=list)
    verb_overrides: dict = field(default_factory=dict)
    # Capability names this command needs (e.g. ["git"]). The availability-
    # aware dispatcher (ADR-047) hides/disables the command when any are
    # unmet. Empty = no external dependencies (always available).
    requires: list[str] = field(default_factory=list)


@dataclass
class ProviderDef:
    """A publisher provider registered by an extension."""

    type: str  # "generation", "storage", "notification", etc.
    name: str
    module: str


@dataclass
class ExtractorDef:
    """A sense extractor registered by an extension."""

    name: str
    module: str
    file_patterns: list[str] = field(default_factory=list)


@dataclass
class MCPServerDef:
    """An MCP server bundled with an extension."""

    name: str
    type: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Skill:
    """An agent skill defined by SKILL.md (Agent Skills standard)."""

    name: str  # Directory name
    path: Path  # Path to SKILL.md
    description: str = ""  # Parsed from frontmatter or first heading


@dataclass
class ConnectionDef:
    """An external connection declared by an extension via [[connections]] in TOML.

    Connections are the unit of external integration in the platform. The platform
    handles credential resolution, health checking, installation prompts,
    service lifecycle, tab completion, and status display. Extension builders
    declare connections; the platform does the rest.

    Attributes:
        name: Unique identifier (e.g., "github", "ollama"). Used in
            ``get_credential(name)``, ``axi connect <name>``, tab completion.
        display_name: Human-readable label for UI (e.g., "GitHub", "Ollama").
        kind: Integration pattern — determines setup flow and health check behavior.
            "api" (REST/GraphQL), "browser" (Playwright sessions), "mcp" (Model
            Context Protocol servers), "a2a" (agent-to-agent federation), "cli"
            (local binary on PATH).
        category: Logical grouping for display ordering in ``axi connect`` and
            ``axi status``. Common values: "llm", "code", "data", "storage",
            "tools", "communication".
        endpoint: Service address. URL for APIs, host:port for TCP, binary name
            for CLI tools (e.g., "ollama", "pandoc").
        transport: Wire protocol (reserved for future use). E.g., "https", "grpc",
            "stdio", "playwright".
        credential_type: What kind of credential this connection uses.
            "api_key" (default), "none" (CLI tools), "browser_session",
            "oauth_token", "mtls".
        credential_env_var: Environment variable name for the credential
            (e.g., "GITHUB_TOKEN"). First source in the resolution chain.
        credential_file: Path relative to ``~/.neut/credentials/`` for file-based
            credential storage (e.g., "teams/state.json").
        required: If True, ``axi status`` warns when this connection is missing.
        health_check: How to verify the connection is working.
            "http_get" (GET health_endpoint, check 200), "tcp_connect" (TCP to
            endpoint, 1s timeout), "cli_version" (run binary --version), "custom"
            (registered Python callable).
        health_endpoint: Target for health check if different from endpoint
            (e.g., "https://api.github.com" when endpoint is the base URL).
        auto_refresh: Reserved. Whether credentials can be refreshed automatically
            (e.g., OAuth token refresh).
        docs_url: URL where users can get credentials or install instructions.
            Shown in ``axi connect <name>`` setup flow.
        post_setup_module: Dotted Python module path for one-time setup hook.
            Called interactively by ``axi connect <name>`` after installation.
        post_setup_function: Function name in post_setup_module. Signature:
            ``() -> int`` (0 = success, 1 = failure). May prompt the user.
        ensure_module: Dotted Python module path for auto-start hook.
            Called silently by ``ensure_available(name)`` before first use.
        ensure_function: Function name in ensure_module. Signature:
            ``() -> bool`` (True = available). Must never prompt or print.
        install_commands: Platform-specific install commands. Keys are platform
            names ("macos", "linux", "windows", "default"). Values are shell
            commands (e.g., "brew install ollama").
        capabilities: What operations this connection supports. List of strings
            from: "read", "write", "admin", "stream". Shown in status output
            and used by agents to determine what actions are available.
    """

    name: str
    display_name: str = ""
    kind: str = "api"
    category: str = ""
    endpoint: str = ""
    transport: str = ""
    credential_type: str = "api_key"
    credential_env_var: str = ""
    credential_file: str = ""
    required: bool = False
    health_check: str = ""
    health_endpoint: str = ""
    auto_refresh: bool = False
    docs_url: str = ""
    post_setup_module: str = ""
    post_setup_function: str = ""
    ensure_module: str = ""
    ensure_function: str = ""
    install_commands: dict[str, str] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    vpn_name: str = ""
    vpn_connect_guide: str = ""
    auth_methods: list[dict[str, str]] = field(default_factory=list)


@dataclass
class WatcherDef:
    """A watcher declared via [[agent.watchers]] in extension TOML."""

    name: str
    enabled: bool = True
    interval: int = 30  # seconds
    path: str = ""
    folder: str = ""
    module: str = ""
    function: str = ""
    cooldown: int = 0


@dataclass
class HookDef:
    """A hook declared via `[[extension.provides]] kind = "hook"`.

    See `docs/specs/spec-hooks.md` §5a. The runtime `HookRegistry` routes
    each `events` entry to either `HookBus.register` (interceptor events)
    or `EventBus.subscribe` (observer events) based on the closed
    taxonomy in `axiom.infra.hooks.event_schemas`.
    """

    events: list[str] = field(default_factory=list)
    entry: str = ""  # "module.path:funcname"
    priority: int = 100
    fail_mode: str = "abort"  # "abort" | "warn" | "ignore"
    description: str = ""


@dataclass
class SafetyCheckDef:
    """A safety/integrity check declared via `[[extension.provides]] kind = "safety_check"`.

    Picked up by TRIAGE's heartbeat sweep. Each registered check is a callable
    `() -> Iterable[Finding]` (or a single Finding) — the function is called on
    each TRIAGE tick (or per the declared schedule). Findings are aggregated
    into ``~/.axi/agents/triage/sweep.jsonl``.

    Extension authors register checks here to extend TRIAGE's safety+integrity
    surface; TRIAGE ships its own built-ins (state-dir disk space, pending-patch
    staleness) via the same mechanism so there is exactly one extension surface.

    See ``axiom.extensions.builtins.diagnostics.safety`` for the Finding shape
    and the built-in checks.
    """

    name: str  # short identifier; should be ext-prefixed for collision avoidance
    entry: str  # "module.path:funcname" — callable returning Finding(s)
    schedule: str = "every_heartbeat"  # "every_heartbeat" | "daily" (Phase 1: every_heartbeat only)
    severity_default: str = "warning"  # "info" | "warning" | "critical"
    description: str = ""


@dataclass
class AgentConfig:
    """Agent lifecycle configuration from [agent] in extension TOML.

    Aligns with OpenClaw HEARTBEAT.md pattern — declares how the agent
    runs as a persistent service.
    """

    heartbeat_interval: int = 300  # seconds
    startup: str = "lazy"  # "lazy" | "eager" | "daemon"
    watchers: list[WatcherDef] = field(default_factory=list)
    routines_md: str | None = None  # Contents of ROUTINES.md if present

    # Concrete command the scheduler should run on each heartbeat tick.
    # Space-separated argv, passed after the agent's CLI noun — e.g.
    # "health --json" becomes `axi hygiene stat health --json`. Empty means the
    # agent has no production-ready heartbeat yet; register_all_daemon_agents
    # will SKIP rather than crash-loop a nonexistent subcommand (which is
    # exactly what broke v0.9.0–v0.10.2 where every daemon agent was
    # configured to invoke `<noun> heartbeat` even though no such
    # subcommand existed).
    heartbeat_command: str = ""

    # Optional: env vars to inject into the persistent service definition.
    # Values may reference shell-style ${VAR} which is substituted at install
    # time from os.environ. The bounded service-env contract (ADR-036 §D9)
    # treats this as the agent's auditable, manifest-declared opt-in path
    # for any env beyond bounded PATH + LANG/LC_ALL. HOME is the canonical
    # use case: an agent that shells out to gh / glab / other tools that
    # read user config from ~/ must declare `HOME = "${HOME}"` here.
    env: dict[str, str] = field(default_factory=dict)

    @property
    def is_always_on(self) -> bool:
        """Whether this agent should run as a persistent system service."""
        return self.startup in ("daemon", "eager")

    @property
    def is_registrable(self) -> bool:
        """Whether we have enough info to safely register this agent.

        Daemon agents without a heartbeat_command are NOT registrable —
        registering them would crash-loop on a nonexistent subcommand.
        """
        return self.is_always_on and bool(self.heartbeat_command.strip())

    def service_label(self, agent_name: str) -> str:
        """Platform service label for this agent."""
        return f"com.axiom.{agent_name}-agent"


@dataclass
class Extension:
    """A discovered extension with its parsed manifest."""

    name: str
    version: str
    description: str
    author: str
    root: Path  # Absolute path to extension directory

    # Capability declarations from manifest
    chat_tools_module: str = ""  # e.g. "tools_ext"
    skills_dir: str = "skills"
    cli_commands: list[CLICommandDef] = field(default_factory=list)
    providers: list[ProviderDef] = field(default_factory=list)
    extractors: list[ExtractorDef] = field(default_factory=list)
    safety_checks: list[SafetyCheckDef] = field(default_factory=list)
    mcp_servers: dict[str, MCPServerDef] = field(default_factory=dict)
    connections: list[ConnectionDef] = field(default_factory=list)
    prompt_contributions: list = field(default_factory=list)  # list[PromptContributionDef]
    hooks: list[HookDef] = field(default_factory=list)
    # spec-settings §4.1 [[settings.sections]] — declared per-extension,
    # consumed by axiom.infra.settings_sections.discover_settings_sections.
    settings_sections: list = field(default_factory=list)  # list[SettingsSectionDef]

    # Classification
    kind: str = "tool"  # "agent", "tool", or "utility"
    module_group: str = ""  # PRD-level grouping (e.g. "platform", "operations")

    # Agent lifecycle (OpenClaw alignment)
    agent: AgentConfig | None = None

    # Runtime state
    enabled: bool = True
    builtin: bool = False  # True for extensions shipped inside extensions/builtins/
    skills: list[Skill] = field(default_factory=list)

    @property
    def manifest_path(self) -> Path:
        return self.root / "axiom-extension.toml"

    @property
    def capabilities(self) -> list[str]:
        """Human-readable list of what this extension provides."""
        caps = []
        if self.chat_tools_module:
            caps.append("chat tools")
        if self.skills:
            caps.append(f"{len(self.skills)} skill(s)")
        if self.cli_commands:
            caps.append(f"{len(self.cli_commands)} CLI command(s)")
        if self.providers:
            caps.append(f"{len(self.providers)} provider(s)")
        if self.extractors:
            caps.append(f"{len(self.extractors)} extractor(s)")
        if self.mcp_servers:
            caps.append(f"{len(self.mcp_servers)} MCP server(s)")
        if self.connections:
            caps.append(f"{len(self.connections)} connection(s)")
        if self.agent and self.agent.is_always_on:
            caps.append("always-on daemon")
        return caps


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------

MANIFEST_FILENAME = "axiom-extension.toml"


def parse_manifest(manifest_path: Path) -> Extension:
    """Parse a axiom-extension.toml file into an Extension object.

    Raises ValueError if required fields are missing.
    Raises FileNotFoundError if the manifest doesn't exist.
    """
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    from axiom.infra.toml_compat import tomllib

    text = manifest_path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    root = manifest_path.parent

    ext_section = data.get("extension", {})
    name = ext_section.get("name", "")
    if not name:
        raise ValueError(f"Missing [extension].name in {manifest_path}")

    ext = Extension(
        name=name,
        version=ext_section.get("version", "0.1.0"),
        description=ext_section.get("description", ""),
        author=ext_section.get("author", ext_section.get("owner", "")),
        root=root,
        builtin=ext_section.get("builtin", False),
        kind=ext_section.get("kind", "tool"),
        module_group=ext_section.get("module", ""),
    )

    # Chat tools
    chat_section = data.get("chat_tools", {})
    ext.chat_tools_module = chat_section.get("module", "")

    # Skills
    skills_section = data.get("skills", {})
    ext.skills_dir = skills_section.get("dir", "skills")

    # CLI commands — AEOS canonical form. The legacy `[[cli.commands]]`
    # bridge is gone (per the no-shim rule). Manifests must declare
    # `[[extension.provides]] kind = "cmd"` with `entry = "module:func"`.
    for prov in ext_section.get("provides", []):
        if prov.get("kind") != "cmd":
            continue
        entry = prov.get("entry", "")
        if ":" in entry:
            module_path, function_name = entry.split(":", 1)
        else:
            # Tolerate bare-module entries during the staged migration —
            # default the function name to `main`.
            module_path = entry
            function_name = "main"
        ext.cli_commands.append(
            CLICommandDef(
                noun=prov.get("noun", ""),
                module=module_path,
                description=prov.get("description", ""),
                function=function_name or "main",
                entry=entry,
                tier=prov.get("tier", "core"),
                intent_groups=list(prov.get("intent_groups", [])),
                verb_overrides=dict(prov.get("verb_overrides", {})),
                requires=list(prov.get("requires", [])),
            )
        )

    # Safety checks — AEOS [[extension.provides]] blocks with kind="safety_check"
    # Picked up by TRIAGE's heartbeat sweep. Built-in checks TRIAGE ships use the
    # same registration mechanism as third-party extension checks.
    for prov in ext_section.get("provides", []):
        if prov.get("kind") != "safety_check":
            continue
        ext.safety_checks.append(
            SafetyCheckDef(
                name=prov.get("name", ""),
                entry=prov.get("entry", ""),
                schedule=prov.get("schedule", "every_heartbeat"),
                severity_default=prov.get("severity_default", "warning"),
                description=prov.get("description", ""),
            )
        )

    # Hooks — AEOS [[extension.provides]] blocks with kind="hook"
    for prov in ext_section.get("provides", []):
        if prov.get("kind") != "hook":
            continue
        events = list(prov.get("events", []) or [])
        if not events:
            continue
        ext.hooks.append(
            HookDef(
                events=events,
                entry=prov.get("entry", ""),
                priority=int(prov.get("priority", 100)),
                fail_mode=prov.get("fail_mode", "abort"),
                description=prov.get("description", ""),
            )
        )

    # Providers
    for prov in data.get("providers", []):
        ext.providers.append(
            ProviderDef(
                type=prov.get("type", ""),
                name=prov.get("name", ""),
                module=prov.get("module", ""),
            )
        )

    # Extractors
    for extr in data.get("extractors", []):
        ext.extractors.append(
            ExtractorDef(
                name=extr.get("name", ""),
                module=extr.get("module", ""),
                file_patterns=extr.get("file_patterns", []),
            )
        )

    # Prompt contributions (#84) — declarative layer contributions
    from axiom.infra.prompt_contributions import PromptContributionDef

    for pc in data.get("prompt_contributions", []):
        if not pc.get("layer") or not pc.get("name"):
            continue
        ext.prompt_contributions.append(
            PromptContributionDef(
                layer=pc.get("layer", ""),
                name=pc.get("name", ""),
                source_module=pc.get("source_module", ""),
                source_function=pc.get("source_function", ""),
                required=bool(pc.get("required", False)),
            )
        )

    # MCP servers
    for key, val in data.get("mcp_servers", {}).items():
        if isinstance(val, dict):
            ext.mcp_servers[key] = MCPServerDef(
                name=key,
                type=val.get("type", "stdio"),
                command=val.get("command", ""),
                args=val.get("args", []),
                env=val.get("env", {}),
            )

    # Connections
    for conn_data in data.get("connections", []):
        conn_name = conn_data.get("name", "")
        if conn_name:
            ext.connections.append(
                ConnectionDef(
                    name=conn_name,
                    display_name=conn_data.get("display_name", conn_name),
                    kind=conn_data.get("kind", "api"),
                    category=conn_data.get("category", ""),
                    endpoint=conn_data.get("endpoint", ""),
                    transport=conn_data.get("transport", ""),
                    credential_type=conn_data.get("credential_type", "api_key"),
                    credential_env_var=conn_data.get("credential_env_var", ""),
                    credential_file=conn_data.get("credential_file", ""),
                    required=conn_data.get("required", False),
                    health_check=conn_data.get("health_check", ""),
                    health_endpoint=conn_data.get("health_endpoint", ""),
                    auto_refresh=conn_data.get("auto_refresh", False),
                    docs_url=conn_data.get("docs_url", ""),
                    post_setup_module=conn_data.get("post_setup_module", ""),
                    post_setup_function=conn_data.get("post_setup_function", ""),
                    ensure_module=conn_data.get("ensure_module", ""),
                    ensure_function=conn_data.get("ensure_function", ""),
                    install_commands=conn_data.get("install_commands", {}),
                    capabilities=conn_data.get("capabilities", []),
                    vpn_name=conn_data.get("vpn_name", ""),
                    vpn_connect_guide=conn_data.get("vpn_connect_guide", ""),
                    auth_methods=conn_data.get("auth_methods", []),
                )
            )

    # Agent lifecycle
    agent_section = data.get("agent")
    if agent_section is not None:
        watchers = []
        for w in agent_section.get("watchers", []):
            watchers.append(
                WatcherDef(
                    name=w.get("name", ""),
                    enabled=w.get("enabled", True),
                    interval=w.get("interval", 30),
                    path=w.get("path", ""),
                    folder=w.get("folder", ""),
                    module=w.get("module", ""),
                    function=w.get("function", ""),
                    cooldown=w.get("cooldown", 0),
                )
            )

        # Load ROUTINES.md if present (OpenClaw HEARTBEAT.md equivalent)
        routines_path = root / "ROUTINES.md"
        routines_md = None
        if routines_path.is_file():
            try:
                routines_md = routines_path.read_text(encoding="utf-8")
            except OSError:
                pass

        env_section = agent_section.get("env", {})
        if not isinstance(env_section, dict):
            env_section = {}
        # Coerce values to str — TOML allows ints/bools but env must be str.
        agent_env = {str(k): str(v) for k, v in env_section.items()}

        ext.agent = AgentConfig(
            heartbeat_interval=agent_section.get("heartbeat_interval", 300),
            startup=agent_section.get("startup", "lazy"),
            heartbeat_command=agent_section.get("heartbeat_command", ""),
            env=agent_env,
            watchers=watchers,
            routines_md=routines_md,
        )

    # Scan for skills (SKILL.md files)
    skills_path = root / ext.skills_dir
    if skills_path.is_dir():
        ext.skills = _scan_skills(skills_path)

    # spec-settings §4.1 [[settings.sections]] — parsed lazily to keep
    # the contracts module free of the gateway-side data classes.
    from axiom.infra.settings_sections import parse_settings_sections
    ext.settings_sections = parse_settings_sections(data)

    return ext


def _scan_skills(skills_dir: Path) -> list[Skill]:
    """Scan a directory for SKILL.md files (Agent Skills standard)."""
    skills = []
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        skill_name = skill_md.parent.name
        description = _parse_skill_description(skill_md)
        skills.append(Skill(name=skill_name, path=skill_md, description=description))
    return skills


def _parse_skill_description(skill_md: Path) -> str:
    """Extract description from SKILL.md frontmatter or first paragraph."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""

    # Try YAML frontmatter (--- delimited)
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            frontmatter = text[3:end]
            for line in frontmatter.split("\n"):
                line = line.strip()
                if line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip()
                    return desc.strip("\"'")

    # Fall back to first non-heading, non-empty line
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        return line

    return ""


def validate_extension(ext: Extension) -> list[str]:
    """Validate an extension's manifest and file structure.

    Returns a list of issues (empty = valid).
    Builtins verify importability; user extensions verify filesystem paths.
    """
    issues: list[str] = []

    if not ext.name:
        issues.append("Extension name is required")

    if not ext.root.is_dir():
        issues.append(f"Extension root not found: {ext.root}")
        return issues  # Can't check further

    if not ext.manifest_path.exists():
        issues.append(f"Manifest not found: {ext.manifest_path}")

    if ext.builtin:
        # Builtins: verify CLI modules are importable
        import importlib.util

        for cmd in ext.cli_commands:
            try:
                spec = importlib.util.find_spec(cmd.module)
            except (ModuleNotFoundError, ValueError):
                spec = None
            if spec is None:
                issues.append(f"Builtin CLI module not importable: {cmd.module}")
    else:
        # User extensions: verify filesystem paths

        # Check chat tools module exists
        if ext.chat_tools_module:
            tools_dir = ext.root / ext.chat_tools_module.replace(".", "/")
            if not tools_dir.is_dir():
                issues.append(f"Chat tools module dir not found: {tools_dir}")

        # Check CLI command modules exist
        for cmd in ext.cli_commands:
            mod_path = ext.root / cmd.module.replace(".", "/")
            if not mod_path.with_suffix(".py").exists() and not (mod_path / "__init__.py").exists():
                issues.append(f"CLI module not found: {cmd.module}")

        # Check provider modules exist
        for prov in ext.providers:
            mod_path = ext.root / prov.module.replace(".", "/")
            if not mod_path.with_suffix(".py").exists():
                issues.append(f"Provider module not found: {prov.module}")

        # Check extractor modules exist
        for extr in ext.extractors:
            mod_path = ext.root / extr.module.replace(".", "/")
            if not mod_path.with_suffix(".py").exists():
                issues.append(f"Extractor module not found: {extr.module}")

    # Check skills have SKILL.md
    skills_path = ext.root / ext.skills_dir
    if skills_path.is_dir():
        for skill_dir in skills_path.iterdir():
            if skill_dir.is_dir() and not (skill_dir / "SKILL.md").exists():
                issues.append(f"Skill dir missing SKILL.md: {skill_dir.name}")

    return issues
