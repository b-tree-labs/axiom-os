# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI Registry — Single source of truth for all axi commands.

This module dynamically discovers CLI commands by introspecting argparse parsers.
No manual sync needed: add a command to a CLI module, it appears everywhere.

Usage:
    from axiom.cli_registry import get_all_commands, get_slash_commands

    # Get all commands for help/docs
    commands = get_all_commands()

    # Get slash commands for chat REPL
    slash_commands = get_slash_commands()

    # Execute a command programmatically
    from axiom.cli_registry import execute_command
    result = execute_command("sense", "status", [])
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CLICommand:
    """A single CLI command definition."""

    namespace: str          # e.g., "sense", "doc"
    name: str               # e.g., "ingest", "status"
    help: str               # e.g., "Run extractors on inbox data"
    arguments: list[dict] = field(default_factory=list)  # Argument definitions
    handler: Callable | None = None  # Function to call

    @property
    def full_name(self) -> str:
        """Full command name like 'sense ingest'."""
        return f"{self.namespace} {self.name}"

    @property
    def slash_name(self) -> str:
        """Slash command name like '/sense ingest'."""
        return f"/{self.full_name}"

    def to_slash_entry(self) -> tuple[str, str]:
        """Return (slash_name, help) for SLASH_COMMANDS dict."""
        return (self.slash_name, self.help)


@dataclass
class CLINamespace:
    """A CLI namespace (e.g., 'sense', 'doc')."""

    name: str
    description: str
    commands: dict[str, CLICommand] = field(default_factory=dict)

    def add_command(self, cmd: CLICommand) -> None:
        self.commands[cmd.name] = cmd


# ---------------------------------------------------------------------------
# Parser Introspection
# ---------------------------------------------------------------------------

def _extract_commands_from_parser(
    parser: argparse.ArgumentParser,
    namespace: str,
) -> list[CLICommand]:
    """Extract CLICommand objects from an argparse parser with subparsers."""
    commands = []

    # Find subparsers action
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            # action.choices is a dict of {name: subparser}
            for name, subparser in action.choices.items():
                # Extract arguments from subparser
                arguments = []
                for sub_action in subparser._actions:
                    if isinstance(sub_action, argparse._HelpAction):
                        continue
                    arg_info = {
                        "name": sub_action.dest,
                        "help": sub_action.help or "",
                        "required": sub_action.required if hasattr(sub_action, 'required') else False,
                    }
                    if hasattr(sub_action, 'choices') and sub_action.choices:
                        arg_info["choices"] = list(sub_action.choices)
                    if hasattr(sub_action, 'default') and sub_action.default is not None:
                        arg_info["default"] = sub_action.default
                    arguments.append(arg_info)

                cmd = CLICommand(
                    namespace=namespace,
                    name=name,
                    help=subparser.description or action.choices[name].format_help().split('\n')[0] or "",
                    arguments=arguments,
                )
                # Try to get help from the subparser's prog help
                if hasattr(action, '_parser_class'):
                    pass  # Could extract more info here
                # Use the help= from add_parser if available
                for choice_action in action._choices_actions:
                    if choice_action.dest == name:
                        cmd = CLICommand(
                            namespace=namespace,
                            name=name,
                            help=choice_action.help or "",
                            arguments=arguments,
                        )
                        break
                commands.append(cmd)

    return commands


def _get_parser_for_module(module_path: str) -> argparse.ArgumentParser | None:
    """Import a CLI module and extract its parser."""
    try:
        import importlib
        module = importlib.import_module(module_path)

        # Try common patterns for getting the parser
        if hasattr(module, 'get_parser'):
            return module.get_parser()
        if hasattr(module, 'create_parser'):
            return module.create_parser()
        if hasattr(module, 'build_parser'):
            return module.build_parser()

        # Try to extract from main() by inspecting source
        # This is a fallback - better to add get_parser() to each module
        return None

    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Core CLI modules (always available)
_CORE_CLI_MODULES = {
    "config": "axiom.setup.cli",
    "ext": "axiom.extensions.cli",
}


def _get_cli_modules() -> dict[str, str]:
    """Build CLI_MODULES from core + extension discovery (lazy, cached)."""
    modules = dict(_CORE_CLI_MODULES)
    try:
        from axiom.extensions.discovery import discover_cli_commands

        for noun, info in discover_cli_commands().items():
            if noun not in modules:
                modules[noun] = info["module"]
    except Exception:
        pass
    return modules


# Backwards-compatible name
CLI_MODULES = _get_cli_modules()

# Cache for discovered commands
_command_cache: dict[str, CLINamespace] = {}
_initialized = False


def _initialize_registry() -> None:
    """Discover all commands from CLI modules."""
    global _initialized

    if _initialized:
        return

    for namespace, module_path in CLI_MODULES.items():
        try:
            import importlib
            module = importlib.import_module(module_path)

            # Check if module exposes get_parser()
            if hasattr(module, 'get_parser'):
                parser = module.get_parser()
                commands = _extract_commands_from_parser(parser, namespace)
                ns = CLINamespace(name=namespace, description=parser.description or "")
                for cmd in commands:
                    ns.add_command(cmd)
                _command_cache[namespace] = ns

            # Check if module exposes COMMANDS constant
            elif hasattr(module, 'COMMANDS'):
                ns = CLINamespace(name=namespace, description="")
                for name, help_text in module.COMMANDS.items():
                    cmd = CLICommand(namespace=namespace, name=name, help=help_text)
                    ns.add_command(cmd)
                _command_cache[namespace] = ns

        except ImportError:
            # Module not available, skip
            pass

    _initialized = True


def get_all_commands() -> dict[str, CLINamespace]:
    """Get all registered CLI commands, grouped by namespace."""
    _initialize_registry()
    return _command_cache.copy()


def get_namespace(name: str) -> CLINamespace | None:
    """Get a specific namespace."""
    _initialize_registry()
    return _command_cache.get(name)


def get_command(namespace: str, command: str) -> CLICommand | None:
    """Get a specific command."""
    ns = get_namespace(namespace)
    if ns:
        return ns.commands.get(command)
    return None


def get_slash_commands() -> dict[str, str]:
    """Get all commands formatted as slash commands for chat.

    Returns:
        Dict mapping slash command (e.g., "/sense status") to help text.
    """
    _initialize_registry()

    result = {}
    for ns in _command_cache.values():
        for cmd in ns.commands.values():
            slash_name, help_text = cmd.to_slash_entry()
            result[slash_name] = help_text

    return result


def get_flat_command_list() -> list[str]:
    """Get a flat list of all command names for fuzzy matching."""
    _initialize_registry()

    result = []
    for ns in _command_cache.values():
        for cmd in ns.commands.values():
            result.append(cmd.full_name)
            result.append(cmd.name)  # Also include bare name

    return result


# ---------------------------------------------------------------------------
# Command Execution
# ---------------------------------------------------------------------------

def execute_command(
    namespace: str,
    command: str,
    args: list[str],
    capture_output: bool = False,
) -> dict[str, Any]:
    """Execute a CLI command programmatically.

    Args:
        namespace: CLI namespace (e.g., "sense")
        command: Command name (e.g., "status")
        args: Additional arguments
        capture_output: If True, capture stdout/stderr

    Returns:
        Dict with 'success', 'output', 'error' keys
    """
    modules = _get_cli_modules()
    module_path = modules.get(namespace)
    if not module_path:
        return {"success": False, "error": f"Unknown namespace: {namespace}"}

    try:
        import contextlib
        import importlib
        from io import StringIO

        module = importlib.import_module(module_path)

        # Build argv for the CLI
        old_argv = sys.argv
        sys.argv = [f"axi {namespace}", command] + args

        result = {"success": True, "output": "", "error": ""}

        if capture_output:
            stdout_capture = StringIO()
            stderr_capture = StringIO()
            with contextlib.redirect_stdout(stdout_capture), \
                 contextlib.redirect_stderr(stderr_capture):
                try:
                    module.main()
                except SystemExit as e:
                    if e.code and e.code != 0:
                        result["success"] = False
            result["output"] = stdout_capture.getvalue()
            result["error"] = stderr_capture.getvalue()
        else:
            try:
                module.main()
            except SystemExit as e:
                if e.code and e.code != 0:
                    result["success"] = False

        sys.argv = old_argv
        return result

    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Help Generation
# ---------------------------------------------------------------------------

def generate_help_text(include_args: bool = False) -> str:
    """Generate formatted help text for all commands."""
    _initialize_registry()

    lines = []
    for ns_name, ns in sorted(_command_cache.items()):
        lines.append(f"\n{ns_name}:")
        for cmd_name, cmd in sorted(ns.commands.items()):
            lines.append(f"  /{ns_name} {cmd_name}  — {cmd.help}")
            if include_args and cmd.arguments:
                for arg in cmd.arguments:
                    arg_str = f"    --{arg['name']}"
                    if 'choices' in arg:
                        arg_str += f" [{', '.join(str(c) for c in arg['choices'])}]"
                    if arg.get('help'):
                        arg_str += f"  {arg['help']}"
                    lines.append(arg_str)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Convenience: Register parser (for CLI modules to call)
# ---------------------------------------------------------------------------

_parsers: dict[str, argparse.ArgumentParser] = {}


def register_parser(namespace: str, parser: argparse.ArgumentParser) -> None:
    """Register a parser for a namespace. Called by CLI modules."""
    _parsers[namespace] = parser
    # Invalidate cache to force re-discovery
    global _initialized
    _initialized = False


def get_registered_parser(namespace: str) -> argparse.ArgumentParser | None:
    """Get a registered parser by namespace."""
    return _parsers.get(namespace)
