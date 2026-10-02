# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""IDE detection, configuration, and extension pre-staging.

Detects installed IDEs, installs recommended extensions, and writes
workspace configuration. Used by `axi setup` and `axi ide` commands.

Supports: VS Code, Cursor, PyCharm/IntelliJ, Neovim.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class IDEInfo:
    """Detected IDE with its capabilities."""

    name: str
    binary: str
    installed: bool = False
    version: str = ""
    extensions_installed: list[str] = field(default_factory=list)
    extensions_missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "binary": self.binary,
            "installed": self.installed,
            "version": self.version,
            "extensions_installed": self.extensions_installed,
            "extensions_missing": self.extensions_missing,
        }


# Extensions recommended for the Axiom development experience
RECOMMENDED_EXTENSIONS = {
    "vscode": [
        "redhat.vscode-yaml",  # YAML schema validation + autocomplete
        "ms-python.python",  # Python language support
        "ms-python.vscode-pylance",  # Python type checking
        "bierner.markdown-mermaid",  # Mermaid diagram preview
        "tamasfe.even-better-toml",  # TOML syntax (extension manifests)
    ],
    "cursor": [
        "redhat.vscode-yaml",
        "ms-python.python",
        "tamasfe.even-better-toml",
    ],
    "pycharm": [],  # PyCharm bundles these natively
    "nvim": [],  # Neovim uses LSP servers, not extensions
}

_IDE_BINARIES = [
    ("VS Code", "code"),
    ("Cursor", "cursor"),
    ("PyCharm", "pycharm"),
    ("IntelliJ IDEA", "idea"),
    ("Neovim", "nvim"),
]


def detect_ides() -> list[IDEInfo]:
    """Detect all installed IDEs and their extension status."""
    results = []
    for name, binary in _IDE_BINARIES:
        info = IDEInfo(name=name, binary=binary)

        if not shutil.which(binary):
            results.append(info)
            continue

        info.installed = True
        info.version = _get_version(binary)

        # Check extensions for VS Code / Cursor
        ext_key = binary if binary in ("code", "cursor") else ""
        if ext_key in ("code", "cursor"):
            installed = _list_extensions(binary)
            recommended = RECOMMENDED_EXTENSIONS.get(
                ext_key == "cursor" and "cursor" or "vscode", []
            )
            info.extensions_installed = [e for e in recommended if e in installed]
            info.extensions_missing = [e for e in recommended if e not in installed]

        results.append(info)
    return results


def install_extensions(binary: str = "code", extensions: list[str] | None = None) -> list[str]:
    """Install IDE extensions. Returns list of successfully installed extensions.

    Works with VS Code (`code`) and Cursor (`cursor`).
    """
    if not shutil.which(binary):
        return []

    ext_key = "cursor" if binary == "cursor" else "vscode"
    to_install = extensions or RECOMMENDED_EXTENSIONS.get(ext_key, [])
    installed_already = _list_extensions(binary)

    installed = []
    for ext in to_install:
        if ext in installed_already:
            installed.append(ext)
            continue
        try:
            result = subprocess.run(
                [binary, "--install-extension", ext, "--force"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode == 0:
                installed.append(ext)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return installed


def write_workspace_config(
    project_root: Path,
    *,
    schemas: dict[str, str] | None = None,
    python_path: str | None = None,
    extra_settings: dict[str, Any] | None = None,
) -> None:
    """Write .vscode/ workspace configuration for a project.

    Args:
        project_root: Root directory of the project.
        schemas: Map of JSON Schema URI → file glob pattern.
        python_path: Path to Python interpreter (auto-detected if None).
        extra_settings: Additional VS Code settings to merge.
    """
    vscode_dir = project_root / ".vscode"
    vscode_dir.mkdir(exist_ok=True)

    # --- settings.json ---
    settings: dict[str, Any] = {}

    # Python interpreter
    if python_path is None:
        import sys

        python_path = sys.executable
    settings["python.defaultInterpreterPath"] = python_path

    # YAML schemas
    if schemas:
        settings["yaml.schemas"] = schemas

    # File associations — physics code input files
    settings["files.associations"] = {
        "model.yaml": "yaml",
        "*.toml": "toml",
        "*.i": "mcnp",
        "*.inp": "mcnp",
    }

    # Exclude noisy directories from search
    settings["files.exclude"] = {
        "**/__pycache__": True,
        "**/.pytest_cache": True,
        "**/node_modules": True,
        "**/*.egg-info": True,
    }

    if extra_settings:
        settings.update(extra_settings)

    _merge_json(vscode_dir / "settings.json", settings)

    # --- extensions.json ---
    extensions = {"recommendations": RECOMMENDED_EXTENSIONS["vscode"]}
    _merge_json(vscode_dir / "extensions.json", extensions)


def write_neovim_config(project_root: Path, schemas: dict[str, str] | None = None) -> None:
    """Write .nvim.lua or .luarc.json for Neovim yaml-language-server schema association."""
    if not schemas:
        return

    # Write .yamlls.json — picked up by nvim-lspconfig's yamlls
    config = {"yaml.schemas": schemas}
    (project_root / ".yamlls.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )


def write_pycharm_config(project_root: Path, schemas: dict[str, str] | None = None) -> None:
    """Write .idea/ JSON Schema mappings for PyCharm/IntelliJ."""
    if not schemas:
        return

    idea_dir = project_root / ".idea"
    idea_dir.mkdir(exist_ok=True)

    # PyCharm uses jsonSchemas.xml for schema associations
    entries = []
    for schema_uri, file_pattern in schemas.items():
        entries.append(
            f'      <entry key="{schema_uri}">\n'
            f"        <value>\n"
            f"          <list>\n"
            f'            <item value="{file_pattern}" />\n'
            f"          </list>\n"
            f"        </value>\n"
            f"      </entry>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project version="4">\n'
        '  <component name="JsonSchemaMappingsProjectConfiguration">\n'
        "    <state>\n"
        "      <map>\n" + "\n".join(entries) + "\n"
        "      </map>\n"
        "    </state>\n"
        "  </component>\n"
        "</project>\n"
    )
    (idea_dir / "jsonSchemas.xml").write_text(xml, encoding="utf-8")


def install_vim_syntax(target: str = "local") -> list[str]:
    """Install vim/neovim syntax files for physics codes.

    Args:
        target: "local" for this machine, or "user@host" for SSH remote.

    Returns list of installed syntax files.
    """
    syntax_dir = Path(__file__).parent / "syntax"
    vim_files = list(syntax_dir.glob("*.vim"))

    if not vim_files:
        return []

    installed = []

    if target == "local":
        dest = Path.home() / ".vim" / "syntax"
        dest.mkdir(parents=True, exist_ok=True)
        ftdetect = Path.home() / ".vim" / "ftdetect"
        ftdetect.mkdir(parents=True, exist_ok=True)

        for vf in vim_files:
            import shutil

            shutil.copy2(str(vf), str(dest / vf.name))
            installed.append(str(dest / vf.name))

        # Write ftdetect for auto file type detection
        (ftdetect / "mcnp.vim").write_text("au BufRead,BufNewFile *.i,*.inp set filetype=mcnp\n")
        installed.append(str(ftdetect / "mcnp.vim"))
    else:
        # SSH remote — scp the files
        for vf in vim_files:
            rc = subprocess.run(
                ["ssh", target, "mkdir", "-p", "~/.vim/syntax", "~/.vim/ftdetect"],
                capture_output=True,
                timeout=10,
                check=False,
            ).returncode
            if rc != 0:
                continue

            subprocess.run(
                ["scp", str(vf), f"{target}:~/.vim/syntax/{vf.name}"],
                capture_output=True,
                timeout=30,
                check=False,
            )
            installed.append(f"{target}:~/.vim/syntax/{vf.name}")

        # ftdetect
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".vim", delete=False) as f:
            f.write("au BufRead,BufNewFile *.i,*.inp set filetype=mcnp\n")
            f.flush()
            subprocess.run(
                ["scp", f.name, f"{target}:~/.vim/ftdetect/mcnp.vim"],
                capture_output=True,
                timeout=30,
                check=False,
            )
            installed.append(f"{target}:~/.vim/ftdetect/mcnp.vim")
            import os

            os.unlink(f.name)

    return installed


def install_vscode_grammar(project_root: Path) -> bool:
    """Install TextMate grammar for physics codes into .vscode/.

    VS Code picks up grammars from workspace-level configuration.
    """
    syntax_dir = Path(__file__).parent / "syntax"
    tm_file = syntax_dir / "mcnp.tmLanguage.json"
    if not tm_file.exists():
        return False

    vscode_dir = project_root / ".vscode"
    vscode_dir.mkdir(exist_ok=True)

    import shutil

    shutil.copy2(str(tm_file), str(vscode_dir / "mcnp.tmLanguage.json"))
    return True


def setup_ide(
    project_root: Path,
    *,
    schemas: dict[str, str] | None = None,
    auto_install_extensions: bool = True,
) -> dict[str, Any]:
    """One-shot IDE setup: detect, configure, install extensions.

    Returns a summary dict of what was done.
    """
    ides = detect_ides()
    result: dict[str, Any] = {
        "ides_detected": [],
        "extensions_installed": [],
        "configs_written": [],
    }

    for ide in ides:
        if not ide.installed:
            continue
        result["ides_detected"].append(ide.name)

        if ide.binary in ("code", "cursor"):
            write_workspace_config(project_root, schemas=schemas)
            result["configs_written"].append(f".vscode/ ({ide.name})")

            if auto_install_extensions and ide.extensions_missing:
                installed = install_extensions(ide.binary, ide.extensions_missing)
                result["extensions_installed"].extend(installed)

        elif ide.binary == "nvim":
            write_neovim_config(project_root, schemas=schemas)
            result["configs_written"].append(".yamlls.json (Neovim)")

        elif ide.binary in ("pycharm", "idea"):
            write_pycharm_config(project_root, schemas=schemas)
            result["configs_written"].append(".idea/jsonSchemas.xml (PyCharm)")

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_version(binary: str) -> str:
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip().split("\n")[0] if result.stdout else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _list_extensions(binary: str) -> set[str]:
    try:
        result = subprocess.run(
            [binary, "--list-extensions"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return {line.strip().lower() for line in result.stdout.splitlines() if line.strip()}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return set()


def _merge_json(path: Path, new_data: dict) -> None:
    """Merge new_data into an existing JSON file, or create it."""
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    existing.update(new_data)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
