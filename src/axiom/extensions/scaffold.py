# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Extension scaffolding for `axi ext init`.

Creates a complete extension directory with:
  - axiom-extension.toml manifest
  - Example chat tool (tools_ext/data_query.py)
  - SKILL.md for weekly-slides
  - Docflow GenerationProvider stub for .pptx
  - CLI command stub
"""

from __future__ import annotations

from pathlib import Path

from axiom.infra.paths import get_user_state_dir


def scaffold_extension(
    name: str,
    base_dir: Path | None = None,
    *,
    author: str = "",
    description: str = "",
) -> Path:
    """Create a new extension scaffold.

    Args:
        name: Extension name (used as directory name)
        base_dir: Parent directory. Defaults to ~/<state-dir>/extensions/
        author: Author name for manifest
        description: Extension description

    Returns:
        Path to the created extension directory.
    """
    if base_dir is None:
        base_dir = get_user_state_dir() / "extensions"

    ext_dir = base_dir / name
    if ext_dir.exists():
        raise FileExistsError(f"Extension directory already exists: {ext_dir}")

    # Create directory structure
    ext_dir.mkdir(parents=True)
    (ext_dir / "tools_ext").mkdir()
    (ext_dir / "skills" / "weekly-slides").mkdir(parents=True)
    (ext_dir / "providers").mkdir()
    (ext_dir / "cli").mkdir()
    (ext_dir / "extractors").mkdir()

    # Write manifest
    desc = description or f"{name} extension for Axiom"
    (ext_dir / "axiom-extension.toml").write_text(
        _manifest_template(name, author=author, description=desc),
        encoding="utf-8",
    )

    # Chat tool example
    (ext_dir / "tools_ext" / "__init__.py").write_text("", encoding="utf-8")
    (ext_dir / "tools_ext" / "data_query.py").write_text(
        _chat_tool_template(name), encoding="utf-8"
    )

    # Skill: weekly-slides
    (ext_dir / "skills" / "weekly-slides" / "SKILL.md").write_text(
        _skill_template(), encoding="utf-8"
    )

    # Provider stub: pptx generation
    (ext_dir / "providers" / "__init__.py").write_text("", encoding="utf-8")
    (ext_dir / "providers" / "pptx_generation.py").write_text(
        _provider_template(), encoding="utf-8"
    )

    # CLI command stub
    (ext_dir / "cli" / "__init__.py").write_text("", encoding="utf-8")
    (ext_dir / "cli" / "logs.py").write_text(_cli_template(name), encoding="utf-8")

    # Extractor stub
    (ext_dir / "extractors" / "__init__.py").write_text("", encoding="utf-8")
    (ext_dir / "extractors" / "data_log.py").write_text(_extractor_template(), encoding="utf-8")

    return ext_dir


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _manifest_template(name: str, *, author: str = "", description: str = "") -> str:
    owner_line = f'\nowner = "{author}"' if author else ""
    return f'''[extension]
name = "{name}"
version = "0.1.0"
description = "{description}"
license = "Apache-2.0"{owner_line}
aeos_version = "0.1.0"

[extension.compatibility]
python = ">= 3.11"
axiom = ">= 0.10"

# CLI commands — declare as AEOS provides blocks
[[extension.provides]]
kind = "cmd"
noun = "logs"
entry = "cli.logs:main"
description = "Query and analyze operation logs"

# Chat tools — auto-discovered, same contract as core tools_ext/
[chat_tools]
module = "tools_ext"

# Skills — Agent Skills standard (SKILL.md), cross-tool compatible
[skills]
dir = "skills"

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

# External connections — uncomment and customize
# [[connections]]
# name = "my_api"
# display_name = "My API"
# kind = "api"
# credential_env_var = "MY_API_TOKEN"
# category = "data"
# health_check = "http_get"
# health_endpoint = "https://api.example.com/health"
# docs_url = "https://example.com/docs/api-keys"
'''


def _chat_tool_template(ext_name: str) -> str:
    return f'''"""Data query tool for {ext_name}.

Example chat tool — modify this for your own data sources.
"""

from axiom.extensions.builtins.chat.tools import ToolDef
from axiom.infra.orchestrator.actions import ActionCategory

TOOLS = [
    ToolDef(
        name="data_query",
        description="Query operation logs. Returns recent entries matching the query.",
        category=ActionCategory.READ,
        parameters={{
            "type": "object",
            "properties": {{
                "query": {{
                    "type": "string",
                    "description": "Search term or date range (e.g., 'status', '2026-03-01').",
                }},
                "limit": {{
                    "type": "integer",
                    "description": "Max results to return (default: 10).",
                }},
            }},
        }},
    ),
]


def execute(name: str, params: dict) -> dict:
    """Execute tool. Always return a dict."""
    if name == "data_query":
        query = params.get("query", "")
        limit = params.get("limit", 10)
        # TODO: Replace with actual data source query
        return {{
            "query": query,
            "results": [
                {{
                    "timestamp": "2026-03-04T10:00:00Z",
                    "parameter": "system_status",
                    "value": "nominal",
                    "operator": "J. Doe",
                }},
            ],
            "count": 1,
            "note": "Stub data — connect to your log database.",
        }}
    return {{"error": f"Unknown tool: {{name}}"}}
'''


def _skill_template() -> str:
    return """---
name: weekly-slides
description: Generate weekly progress slides from sense data
---

# Weekly Slides

Generate a PowerPoint deck summarizing this week's program activity.

## Instructions

1. Query sense status for the current week's signals
2. Group signals by initiative (Project A, Project B, Cost Estimation, etc.)
3. Generate one slide per active initiative with:
   - Key accomplishments (from progress signals)
   - Blockers (from blocker signals)
   - Next steps (from action_item signals)
4. Add a summary slide at the beginning
5. Add a blockers/risks slide at the end

## Parameters

- **format**: Output format — pptx (default) or pdf
- **week**: ISO date for the target week (default: current week)
- **template**: Path to .pptx template file (optional)

## Example

```
Generate weekly slides for 2026-03-04
```
"""


def _provider_template() -> str:
    return '''"""PowerPoint generation provider for publisher.

Converts markdown source files to .pptx presentations.
Requires python-pptx: pip install python-pptx

This is a stub — implement the generate() method with your
slide-building logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class PptxGenerationProvider:
    """Docflow GenerationProvider for PowerPoint output.

    Implements the same contract as tools.extensions.builtins.publishing.providers.base.GenerationProvider
    without importing it (keeps extension dependency-free).
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    def generate(self, source_path: Path, output_path: Path, options: Any = None) -> dict:
        """Convert markdown to .pptx.

        Args:
            source_path: Path to the markdown source file.
            output_path: Path for the generated .pptx file.
            options: GenerationOptions (toc, watermark, etc.)

        Returns:
            Dict with output_path, format, size_bytes, warnings.
        """
        # TODO: Implement with python-pptx
        # from pptx import Presentation
        # prs = Presentation()
        # ... build slides from markdown sections ...
        # prs.save(str(output_path))

        return {
            "output_path": str(output_path),
            "format": "pptx",
            "size_bytes": 0,
            "warnings": ["Stub provider — implement generate() with python-pptx"],
        }

    def rewrite_links(self, artifact_path: Path, link_map: dict[str, str]) -> None:
        """Rewrite internal links in generated artifact."""
        pass  # Links in pptx are typically external

    def get_output_extension(self) -> str:
        return ".pptx"

    def supports_watermark(self) -> bool:
        return False
'''


def _cli_template(ext_name: str) -> str:
    return '''"""CLI command for querying operation logs.

Registered as: axi logs
"""

from __future__ import annotations

import argparse


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi logs",
        description="Query and analyze operation logs",
    )
    sub = parser.add_subparsers(dest="action")

    search = sub.add_parser("search", help="Search log entries")
    search.add_argument("query", nargs="?", default="", help="Search term")
    search.add_argument("--limit", type=int, default=10, help="Max results")
    search.add_argument("--format", choices=["table", "json"], default="table")

    sub.add_parser("summary", help="Show summary of recent operations")
    sub.add_parser("status", help="Current system status")

    return parser


def main():
    parser = get_parser()
    args = parser.parse_args()

    if args.action == "search":
        print(f"Searching logs for: {args.query or '(all)'}")
        print("  (connect to your log database)")
    elif args.action == "summary":
        print("Operations Summary")
        print("  Last 7 days: 12 entries")
        print("  (connect to your log database)")
    elif args.action == "status":
        print("System Status: NOMINAL")
        print("  (connect to your status feed)")
    else:
        parser.print_help()
'''


def _extractor_template() -> str:
    return '''"""Data log extractor for the sense pipeline.

Extracts signals from operation log files (.log, .csv).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


class DataLogExtractor:
    """Sense extractor for operation logs.

    Implements the same contract as tools.extensions.builtins.signals.extractors.base.BaseExtractor
    without importing it (keeps extension dependency-free).
    """

    @property
    def name(self) -> str:
        return "data_log"

    def can_handle(self, path: Path) -> bool:
        return path.exists() and path.suffix in (".log", ".csv")

    def extract(self, source: Path, **kwargs) -> dict:
        """Extract signals from an operation log file.

        Returns dict matching the Extraction dataclass shape:
            extractor, source_file, signals, errors, extracted_at
        """
        now = datetime.now(timezone.utc).isoformat()

        try:
            text = source.read_text(encoding="utf-8")
        except Exception as e:
            return {
                "extractor": self.name,
                "source_file": str(source),
                "signals": [],
                "errors": [f"Failed to read file: {e}"],
                "extracted_at": now,
            }

        # TODO: Parse log format and create signals
        # Each signal should have: source, timestamp, raw_text,
        #   signal_type, detail, confidence, people, initiatives

        return {
            "extractor": self.name,
            "source_file": str(source),
            "signals": [],
            "errors": [],
            "extracted_at": now,
        }
'''
