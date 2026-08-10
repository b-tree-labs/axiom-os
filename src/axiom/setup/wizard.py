# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Interactive onboarding wizard.

Orchestrates the 6-phase flow:
  PROBE → SUMMARY → CREDENTIALS → CONFIG → TEST → DONE

Each phase auto-saves progress so users can resume with `axi config`.
"""
# pylint: disable=import-outside-toplevel,broad-exception-caught,reimported,redefined-outer-name,subprocess-run-check

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from axiom.infra.branding import get_branding as _get_branding
from axiom.infra.routing_health import (
    collect_classifier_health,
    render_classifier_health,
)
from axiom.rag.health import collect_rag_health, render_rag_health
from axiom.setup import renderer
from axiom.setup.guides import (
    CREDENTIAL_GUIDES,
    CredentialGuide,
    get_llm_guides,
)
from axiom.setup.llamafile import (
    DEFAULT_MODEL as DEFAULT_LOCAL_MODEL,
)
from axiom.setup.llamafile import (
    detect_existing_bonsai_cache,
    resolve_model,
)
from axiom.setup.probe import ProbeResult, run_probe
from axiom.setup.renderer import _c as apply_color
from axiom.setup.renderer import _Colors as Colors
from axiom.setup.state import SetupState, load_state, save_state
from axiom.setup.tester import ChannelTester


def _get_product_name() -> str:
    """Return active product name from branding registry."""
    try:
        return _get_branding().product_name
    except Exception:
        return "Axiom"


def _get_cli_comment() -> str:
    """Return shell alias comment from branding registry."""
    try:
        return _get_branding().shell_comment or "Axiom CLI shortcut"
    except Exception:  # pylint: disable=broad-exception-caught
        return "Axiom CLI shortcut"


# Phase names in order
# infra phase sets up Docker/K3D/PostgreSQL if needed
PHASES = [
    "probe",
    "summary",
    "infra",
    "credentials",
    "config",
    "test",
    "services",
    "community_pack",
    "ide",
    "done",
]


class SetupWizard:
    """Orchestrates the interactive setup flow."""

    def __init__(self, root: Path | None = None, model: str | None = None):
        if root is None:
            from axiom.setup.probe import _find_project_root

            root = _find_project_root()
        self.root = root
        self.state: SetupState = load_state(root) or SetupState()
        self.probe_result: ProbeResult | None = None
        # Explicit local-LLM choice (None = auto: qwen unless migration prompt
        # answers otherwise). Validate eagerly so a bad name fails fast.
        if model is not None:
            resolve_model(model)
        self.requested_model: str | None = model
        self._resolved_model: str | None = None

    # ------------------------------------------------------------------
    # Local LLM model selection
    # ------------------------------------------------------------------

    def resolve_local_model(self) -> str:
        """Decide which local LLM profile to provision.

        - If the caller passed ``--model``, honor it (validated in __init__).
        - Else if a Bonsai-1.7B cache exists in ``~/.axi/llamafile/``:
            - On a TTY: prompt the user to keep it or upgrade to qwen.
              Default = upgrade.
            - Off a TTY (CI/scripts): default to qwen, log a one-line note
              that the existing cache was detected and is being kept on disk.
        - Else default to qwen.

        Result is cached so repeat calls (e.g. status display + provisioning)
        don't re-prompt.
        """
        if self._resolved_model is not None:
            return self._resolved_model

        if self.requested_model is not None:
            self._resolved_model = self.requested_model
            return self._resolved_model

        cached = detect_existing_bonsai_cache()
        if cached is None:
            self._resolved_model = DEFAULT_LOCAL_MODEL
            return self._resolved_model

        # Existing Bonsai cache detected.
        if not sys.stdin.isatty():
            renderer.info(
                f"Detected existing Bonsai-1.7B cache at {cached} — "
                f"defaulting to qwen2.5:7b. Cache kept on disk; "
                f"delete manually if desired."
            )
            self._resolved_model = DEFAULT_LOCAL_MODEL
            return self._resolved_model

        renderer.info(
            f"Found existing Bonsai-1.7B model at {cached}."
        )
        upgrade = renderer.prompt_yn(
            "Upgrade to qwen2.5:7b (4.7GB download)? "
            "[Y = upgrade, n = keep Bonsai]",
            default=True,
        )
        self._resolved_model = "qwen" if upgrade else "bonsai"
        return self._resolved_model

    def run(self) -> None:
        """Run the full wizard, resuming from last saved phase."""
        renderer.banner()

        # Determine starting phase
        start_idx = 0
        if self.state.completed_phases:
            last = self.state.completed_phases[-1]
            if last in PHASES:
                start_idx = PHASES.index(last) + 1

        # All phases already done — show status instead of empty resume
        if start_idx >= len(PHASES):
            renderer.info("Setup already complete. Showing current status.\n")
            self.show_status()
            renderer.blank()
            renderer.info(f"Run '{_get_branding().cli_name} config --reset' to start over.")
            return

        if start_idx > 0:
            renderer.info("Resuming from where you left off...\n")
        else:
            renderer.text("Let's get your environment ready.\n")

        for phase in PHASES[start_idx:]:
            self.state.current_phase = phase
            save_state(self.state, self.root)

            handler = getattr(self, f"_phase_{phase}", None)
            if handler is not None:
                handler()

            self.state.mark_phase_complete(phase)
            save_state(self.state, self.root)

    # ------------------------------------------------------------------
    # Phase: PROBE
    # ------------------------------------------------------------------

    def _phase_probe(self) -> None:
        renderer.heading("Checking your system")
        renderer.text("This takes a few seconds...\n")
        self.probe_result = run_probe(self.root)
        self.state.probe_result = self.probe_result.to_dict()

    # ------------------------------------------------------------------
    # Phase: SUMMARY
    # ------------------------------------------------------------------

    @staticmethod
    def _friendly_os(os_name: str, os_version: str) -> str:
        """Convert OS identifiers to user-friendly names."""
        if os_name == "Darwin":
            return f"macOS {os_version}"
        if os_name == "Windows":
            return f"Windows {os_version}"
        if os_name == "Linux":
            return f"Linux {os_version}"
        return f"{os_name} {os_version}"

    @staticmethod
    def _clean_version(raw: str) -> str:
        """Strip noise from version strings.

        Example: 'git version 2.50.1 (Apple Git-155)' → '2.50.1'.
        """
        match = re.search(r"(\d+\.\d+[\.\d]*)", raw)
        return match.group(1) if match else raw

    def _show_system_info(self, pr: ProbeResult) -> None:
        """Display system info section of the summary."""
        renderer.heading("Your System")
        renderer.status_line(
            "Operating system",
            self._friendly_os(pr.os_name, pr.os_version),
            True,
        )
        renderer.status_line("Python", pr.python_version, True)
        if pr.is_git_repo:
            renderer.status_line("Project", f"Found (branch: {pr.git_branch})", True)
        else:
            renderer.status_line("Project", "Not inside a git repository", False)
        renderer.status_line(
            "Network", "Connected" if pr.dns_available else "No network detected", pr.dns_available
        )
        renderer.blank()

    def _show_dependencies(self, pr: ProbeResult) -> None:
        """Display dependency check section of the summary."""
        renderer.heading("Tools & Libraries")
        for dep in pr.dependencies:
            label = dep.purpose or dep.name
            if dep.found:
                ver = f" ({self._clean_version(dep.version)})" if dep.version else ""
                renderer.status_line(label, f"Ready{ver}", True)
            else:
                tag = "required" if dep.required else "optional"
                renderer.status_line(label, f"Not found ({tag})", not dep.required)
        renderer.blank()

    def _show_config_status(self, pr: ProbeResult) -> None:
        """Display existing config / needs-setup section of the summary."""
        working: list[str] = []
        needs_setup: list[str] = []
        ms_vars = {"MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET", "MS_GRAPH_TENANT_ID"}
        ms_set = all(pr.env_vars_set.get(v) for v in ms_vars)

        for var, is_set in pr.env_vars_set.items():
            if var in ms_vars:
                continue  # handled as a group below
            name = renderer.friendly_name(var)
            (working if is_set else needs_setup).append(name)

        (working if ms_set else needs_setup).append("Microsoft 365 connection")

        if working:
            renderer.heading("Already Configured")
            for item in working:
                renderer.success(item)
        if needs_setup:
            renderer.heading("Needs Setup")
            for item in needs_setup:
                renderer.warning(item)
        renderer.blank()

    def _phase_summary(self) -> None:
        if self.probe_result is None:
            self.probe_result = ProbeResult.from_dict(self.state.probe_result)
        pr = self.probe_result
        self._show_system_info(pr)
        self._show_dependencies(pr)
        self._show_config_status(pr)

    # ------------------------------------------------------------------
    # Phase: INFRA (Docker, K3D, PostgreSQL)
    # ------------------------------------------------------------------

    def _phase_infra(self) -> None:
        """Set up infrastructure with graceful degradation.

        Three paths, selected automatically:
        - **K3D** — full Kubernetes stack (K3D + Docker both present)
        - **Docker Compose** — PostgreSQL via ``docker compose`` (Docker only)
        - **Native** — guide user to install PostgreSQL; use llamafile for LLM
        """
        from axiom.setup.infra import (
            InfraStatus,
            check_docker,
            check_k3d,
            check_neut_cluster,
            detect_infra_path,
            provision_infrastructure,
            run_infra_setup,
        )

        # Quick check if full K3D infrastructure is already ready
        docker = check_docker()
        k3d = check_k3d()

        if docker.status == InfraStatus.READY and k3d.status == InfraStatus.READY:
            cluster = check_neut_cluster()
            if cluster.status == InfraStatus.READY:
                renderer.heading("Infrastructure")
                renderer.success("Docker, K3D, and PostgreSQL already configured")
                renderer.blank()
                return

        # Detect best available path
        infra_path = detect_infra_path()

        renderer.heading("Infrastructure Setup")

        path_descriptions = {
            "k3d": (
                "K3D and Docker detected — full Kubernetes stack available.\n"
                "This provides PostgreSQL + pgvector + local LLM in a K3D cluster.\n"
            ),
            "docker-compose": (
                "Docker detected (no K3D). Will use Docker Compose for PostgreSQL\n"
                "and llamafile for local LLM.\n"
            ),
            "native": (
                "No Docker detected. Will check for native PostgreSQL and use\n"
                "llamafile for local LLM (no containers required).\n"
            ),
        }
        renderer.text(path_descriptions.get(infra_path, ""))

        # Check if user wants to set up infrastructure now
        if not renderer.prompt_yn("Set up local database infrastructure now?", default=True):
            renderer.info("Skipped — you can set this up later with: axi infra")
            self.state.infra_configured = False
            return

        renderer.blank()

        if infra_path == "k3d":
            # Full K3D path — use the existing comprehensive setup
            result = run_infra_setup(
                auto_fix=True,
                interactive=True,
                skip_cluster=False,
            )
            renderer.blank()
            if result.success:
                renderer.success("Infrastructure ready!")
                self.state.infra_configured = True
            else:
                renderer.warning("Some infrastructure components need attention.")
                renderer.text("You can complete setup later with: axi infra")
                self.state.infra_configured = False
        else:
            # Docker Compose or native path
            def _cb(msg: str) -> None:
                renderer.info(msg)

            chosen_model = self.resolve_local_model()
            used_path = provision_infrastructure(callback=_cb, model=chosen_model)
            self.state.user_choices["local_model"] = chosen_model

            renderer.blank()
            if used_path == "docker-compose":
                renderer.success("PostgreSQL running via Docker Compose.")
            elif used_path == "native":
                renderer.info("Check the instructions above to complete PostgreSQL setup.")
            self.state.infra_configured = True
            self.state.user_choices["infra_path"] = used_path

        renderer.blank()

    # ------------------------------------------------------------------
    # Phase: CREDENTIALS (delegates to axi connect)
    # ------------------------------------------------------------------

    def _phase_credentials(self) -> None:
        renderer.heading("Connection Settings")
        renderer.text("Let's set up your connections. You can skip any for now.\n")

        try:
            from axiom.extensions.builtins.connect.cli import setup_connection
            from axiom.infra.connections import get_cli_tool, get_registry, has_credential

            registry = get_registry()
            connections = registry.all()

            if not connections:
                renderer.info("No connections registered yet.")
                return

            # Walk through each connection via axi connect's setup flow
            # Required connections first, then by category
            for conn in sorted(connections, key=lambda c: (not c.required, c.category, c.name)):
                # Skip already-configured connections
                if self.state.credentials_configured.get(conn.name):
                    continue

                if conn.kind == "cli":
                    tool = get_cli_tool(conn.name, registry=registry)
                    if tool:
                        renderer.success(f"{conn.display_name} — {tool.version or 'installed'}")
                        self.state.credentials_configured[conn.name] = True
                        save_state(self.state, self.root)
                        continue
                elif conn.credential_type not in ("none", ""):
                    if has_credential(conn.name, registry=registry):
                        renderer.success(f"{conn.display_name} — already set")
                        self.state.credentials_configured[conn.name] = True
                        save_state(self.state, self.root)
                        continue

                # Delegate to axi connect's interactive setup
                setup_connection(conn.name, registry)
                self.state.credentials_configured[conn.name] = True
                save_state(self.state, self.root)

        except ImportError:
            # Fallback to legacy credential guides if connect module unavailable
            self._phase_credentials_legacy()

    def _phase_credentials_legacy(self) -> None:
        """Legacy credential setup — used if connections module is unavailable."""
        if self.probe_result is None:
            self.probe_result = ProbeResult.from_dict(self.state.probe_result)

        ms_vars = {"MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET", "MS_GRAPH_TENANT_ID"}
        llm_envs = {g.env_var for g in get_llm_guides()}
        llm_guides = [g for g in CREDENTIAL_GUIDES if g.env_var in llm_envs]
        non_ms_guides = [
            g for g in CREDENTIAL_GUIDES if g.env_var not in llm_envs and g.env_var not in ms_vars
        ]
        ms_guides = [g for g in CREDENTIAL_GUIDES if g.env_var in ms_vars]

        for guide in llm_guides + non_ms_guides:
            if self.state.credentials_configured.get(guide.env_var):
                continue
            if self.probe_result.env_vars_set.get(guide.env_var):
                renderer.success(f"{guide.display_name} — already set")
                self.state.credentials_configured[guide.env_var] = True
                save_state(self.state, self.root)
                continue
            self._configure_credential(guide)

        ms_all_set = all(
            self.state.credentials_configured.get(g.env_var)
            or self.probe_result.env_vars_set.get(g.env_var)
            for g in ms_guides
        )
        if not ms_all_set:
            self._configure_ms365_group(ms_guides)

    def _configure_ms365_group(self, guides: list[CredentialGuide]) -> None:
        """Walk through all 3 MS 365 credentials as one grouped section."""
        renderer.divider()
        renderer.text(f"\n  {apply_color(Colors.BOLD, 'Microsoft 365 connection')} (required)")
        renderer.text("  Enables file sharing, document storage, and team collaboration.")
        renderer.text("  This needs 3 values from the Azure Portal.\n")

        if not renderer.prompt_yn("Set up Microsoft 365 now?", default=False):
            _cli = _get_branding().cli_name
            renderer.info(
                f"Skipped — you can set this up later with: {_cli} config --set ms_graph_client_id"
            )
            for g in guides:
                self.state.credentials_configured[g.env_var] = False
            save_state(self.state, self.root)
            return

        # Show combined steps
        url = "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps"
        renderer.numbered_steps(
            [
                "Go to the Azure Portal",
                'Navigate to "App registrations" and create a new registration',
                "From the Overview page, copy the Application (client) ID",
                "Copy the Directory (tenant) ID from the same page",
                'Go to "Certificates & secrets" → "New client secret" and copy the Value',
            ]
        )
        renderer.blank()
        renderer.text(f"  Link: {apply_color(Colors.DIM, url)}")
        renderer.blank()

        # Offer to open browser once for all three
        if renderer.prompt_yn("Open this page in your browser?"):
            webbrowser.open(url)
            renderer.blank()
            renderer.info("A page should have opened in your browser.")
            renderer.info("Follow the steps above, then come back here to paste each value.")
            renderer.blank()

        # Prompt for each value
        for guide in guides:
            env_var_set = (
                self.probe_result.env_vars_set.get(guide.env_var) if self.probe_result else False
            )
            if self.state.credentials_configured.get(guide.env_var) or env_var_set:
                renderer.success(f"{guide.display_name} — already set")
                self.state.credentials_configured[guide.env_var] = True
                continue

            renderer.blank()
            renderer.text(f"  {apply_color(Colors.BOLD, guide.display_name)}")
            self._prompt_and_save_credential(guide)

    def _configure_credential(self, guide: CredentialGuide) -> None:
        """Walk the user through configuring a single credential."""
        renderer.divider()
        tag = "required" if guide.required else "optional"
        renderer.text(f"\n  {apply_color(Colors.BOLD, guide.display_name)} ({tag})")
        renderer.text(f"  {guide.description}\n")

        if not renderer.prompt_yn(f"Set up {guide.display_name} now?", default=False):
            renderer.info(
                f"Skipped — you can set this up later with: "
                f"{_get_branding().cli_name} config --set {guide.env_var.lower()}"
            )
            self.state.credentials_configured[guide.env_var] = False
            save_state(self.state, self.root)
            return

        # Show steps and URL
        renderer.numbered_steps(guide.steps)
        if guide.url:
            renderer.blank()
            renderer.text(f"  Link: {apply_color(Colors.DIM, guide.url)}")
        renderer.blank()

        # Offer to open URL
        if guide.url and renderer.prompt_yn("Open this page in your browser?"):
            webbrowser.open(guide.url)
            renderer.blank()
            renderer.info("A page should have opened in your browser.")
            renderer.info("Follow the steps above, then come back here to paste the value.")
            renderer.blank()

        self._prompt_and_save_credential(guide)

    def _prompt_and_save_credential(self, guide: CredentialGuide) -> None:
        """Prompt for a credential value with validation and retry."""
        renderer.text("(press Enter with nothing to skip)\n")
        for attempt in range(3):
            value = renderer.prompt_secret(f"Paste your {guide.display_name}")
            if not value:
                renderer.info("Skipped")
                self.state.credentials_configured[guide.env_var] = False
                save_state(self.state, self.root)
                return

            if guide.validate(value):
                self._save_credential(guide.env_var, value)
                renderer.success(f"{guide.display_name} saved")
                self.state.credentials_configured[guide.env_var] = True
                save_state(self.state, self.root)
                return

            remaining = 2 - attempt
            if remaining > 0:
                renderer.warning(
                    f"That doesn't look right. "
                    f"({remaining} {'tries' if remaining > 1 else 'try'} left)"
                )
            else:
                renderer.error("Could not validate — saving anyway. You can fix later.")
                self._save_credential(guide.env_var, value)
                self.state.credentials_configured[guide.env_var] = True
                save_state(self.state, self.root)
                return

    def _save_credential(self, env_var: str, value: str) -> None:
        """Append or update a credential in the .env file."""
        env_path = self.root / ".env"

        # Also set in current process
        os.environ[env_var] = value

        if env_path.exists():
            content = env_path.read_text(encoding="utf-8")
            # Update existing line
            pattern = re.compile(rf"^{re.escape(env_var)}=.*$", re.MULTILINE)
            if pattern.search(content):
                content = pattern.sub(f"{env_var}={value}", content)
                env_path.write_text(content, encoding="utf-8")
                return

        # Append new line
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"\n{env_var}={value}\n")

    # ------------------------------------------------------------------
    # Phase: CONFIG
    # ------------------------------------------------------------------

    def _phase_config(self) -> None:
        renderer.heading("Configuration Files")

        # Check which files already exist
        facility_exists = (self.root / "runtime" / "config" / "facility.toml").exists()
        models_exists = (self.root / "runtime" / "config" / "models.toml").exists()
        publisher_exists = (self.root / ".publisher.yaml").exists()
        claude_exists = (self.root / ".claude" / "context.md").exists()

        all_exist = facility_exists and models_exists and publisher_exists and claude_exists
        if all_exist:
            renderer.info("All configuration files already in place.")
            self.state.config_files_created["facility.toml"] = True
            self.state.config_files_created["models.toml"] = True
            self.state.config_files_created[".publisher.yaml"] = True
            self.state.config_files_created[".claude/context.md"] = True
            renderer.blank()
            return

        renderer.text("Setting up your project configuration.\n")

        # Auto-generate sensible defaults — don't prompt.
        # Users can edit runtime/config/facility.toml later.
        facility_type = "research"
        facility_name = os.environ.get("USER", "default") + "-facility"
        if not facility_exists:
            self.state.user_choices["facility_type"] = facility_type
            self.state.user_choices["facility_name"] = facility_name
            renderer.info(
                f"Facility: {facility_name} (edit runtime/config/facility.toml to change)"
            )

        # Generate only missing config files
        self._generate_facility_toml(facility_name, facility_type)
        self._generate_models_toml()
        self._generate_retention_yaml()
        self._generate_doc_workflow_yaml()
        self._generate_claude_context()

        renderer.blank()

    def _ask_facility_type(self) -> str:
        options = ["Research facility", "Production facility", "Government facility"]
        idx = renderer.prompt_choice("What type of facility are you working with?", options)
        return ["research", "production", "government"][idx]

    def _generate_facility_toml(self, name: str, ftype: str) -> None:
        """Generate facility.toml from template."""
        dest = self.root / "runtime" / "config" / "facility.toml"
        if dest.exists():
            renderer.info("facility.toml already exists — keeping current version")
            self.state.config_files_created["facility.toml"] = True
            return

        template = self.root / "runtime" / "config.example" / "facility.toml"
        if not template.exists():
            renderer.warning("facility.toml template not found — skipping")
            return

        content = template.read_text(encoding="utf-8")
        content = content.replace('name = "Example Facility"', f'name = "{name}"')
        content = content.replace('type = "research"', f'type = "{ftype}"')

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        renderer.success("Created facility.toml")
        self.state.config_files_created["facility.toml"] = True

    def _generate_models_toml(self) -> None:
        """Generate models.toml with detected LLM providers uncommented."""
        dest = self.root / "runtime" / "config" / "models.toml"
        if dest.exists():
            renderer.info("models.toml already exists — keeping current version")
            self.state.config_files_created["models.toml"] = True
            return

        template = self.root / "runtime" / "config.example" / "models.toml"
        if not template.exists():
            renderer.warning("models.toml template not found — skipping")
            return

        content = template.read_text(encoding="utf-8")

        # Uncomment providers based on available keys
        if os.environ.get("ANTHROPIC_API_KEY"):
            # Uncomment the Anthropic provider block
            content = content.replace("# [[gateway.providers]]", "[[gateway.providers]]", 1)
            content = content.replace('# name = "anthropic"', 'name = "anthropic"')
            content = content.replace(
                '# endpoint = "https://api.anthropic.com/v1"',
                'endpoint = "https://api.anthropic.com/v1"',
            )
            content = content.replace(
                '# model = "claude-sonnet-4-20250514"',
                'model = "claude-sonnet-4-20250514"',
            )
            content = content.replace(
                '# api_key_env = "ANTHROPIC_API_KEY"',
                'api_key_env = "ANTHROPIC_API_KEY"',
            )
            content = content.replace("# priority = 1", "priority = 1")

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        renderer.success("Created models.toml")
        self.state.config_files_created["models.toml"] = True

    def _generate_retention_yaml(self) -> None:
        """Generate retention.yaml from template for data lifecycle management."""
        dest = self.root / "runtime" / "config" / "retention.yaml"
        if dest.exists():
            renderer.info("retention.yaml already exists — keeping current version")
            self.state.config_files_created["retention.yaml"] = True
            return

        template = self.root / "runtime" / "config.example" / "retention.yaml"
        if not template.exists():
            renderer.warning("retention.yaml template not found — skipping")
            return

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template, dest)
        renderer.success("Created retention.yaml — data retention policies for TIDY")
        self.state.config_files_created["retention.yaml"] = True

    def _generate_doc_workflow_yaml(self) -> None:
        """Generate .publisher.yaml from template."""
        dest = self.root / ".publisher.yaml"
        if dest.exists():
            renderer.info(".publisher.yaml already exists — keeping current version")
            self.state.config_files_created[".publisher.yaml"] = True
            return

        template = self.root / ".publisher.yaml.example"
        if not template.exists():
            renderer.warning(".publisher.yaml template not found — skipping")
            return

        content = template.read_text(encoding="utf-8")

        # Set storage provider based on MS 365 availability
        has_ms = all(
            os.environ.get(v)
            for v in ["MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET", "MS_GRAPH_TENANT_ID"]
        )
        if not has_ms:
            # Switch to local storage if MS 365 isn't configured
            content = content.replace("provider: onedrive", "provider: local")

        dest.write_text(content, encoding="utf-8")
        renderer.success("Created .publisher.yaml")
        self.state.config_files_created[".publisher.yaml"] = True

    def _generate_claude_context(self) -> None:
        """Generate .claude/context.md from template."""
        dest = self.root / ".claude" / "context.md"
        if dest.exists():
            renderer.info(".claude/context.md already exists — keeping current version")
            self.state.config_files_created[".claude/context.md"] = True
            return

        template = self.root / ".claude.example" / "context.md"
        if not template.exists():
            renderer.warning(".claude/context.md template not found — skipping")
            return

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template, dest)
        renderer.success("Created .claude/context.md — edit this with your details")
        self.state.config_files_created[".claude/context.md"] = True

    # ------------------------------------------------------------------
    # Phase: TEST
    # ------------------------------------------------------------------

    def _phase_test(self) -> None:
        renderer.heading("Testing Connections")
        renderer.text("Verifying each configured connection...\n")

        tester = ChannelTester(self.root)
        results = tester.run_all()

        for i, result in enumerate(results):
            renderer.progress_bar(i + 1, len(results))
            if result.skipped:
                renderer.info(f"{result.display_name}: {result.message}")
            elif result.passed:
                renderer.success(f"{result.display_name}: {result.message}")
            else:
                renderer.error(f"{result.display_name}: {result.message}")

            status = "pass" if result.passed else ("skip" if result.skipped else "fail")
            self.state.test_results[result.channel] = status

        # Store display names for the done phase
        self.state.user_choices["_channel_names"] = {r.channel: r.display_name for r in results}

        save_state(self.state, self.root)
        renderer.blank()

    # ------------------------------------------------------------------
    # Phase: DONE
    # ------------------------------------------------------------------

    def _phase_services(self) -> None:
        """Register always-on agent services (step 5d per PRD)."""
        renderer.heading("Agent Services")

        try:
            from axiom.extensions.builtins.agents.cli import register_all_daemon_agents
        except ImportError:
            renderer.info("Agent services module not available — skipping.")
            return

        results = register_all_daemon_agents()
        if not results:
            renderer.info("No always-on agents configured.")
            return

        renderer.text(f"Registering {len(results)} agent service(s)...\n")
        for r in results:
            if r.ok:
                renderer.success(f"{r.agent_name}: registered ({r.provider})")
            else:
                detail = r.error or "install or start failed"
                renderer.warning(f"{r.agent_name}: registration failed — {detail}")
                renderer.warning(f"  Heal later with: axi agents start {r.agent_name}")

    def _phase_community_pack(self) -> None:
        """Offer community knowledge pack on first run."""
        renderer.heading("Community Knowledge")

        try:
            from axiom.setup.community_pack import offer_community_pack

            offer_community_pack(callback=renderer.text)
        except ImportError:
            renderer.info("Community pack module not available — skipping.")

    def _phase_ide(self) -> None:
        """Detect IDEs and auto-configure workspace + extensions."""
        renderer.heading("IDE Configuration")

        try:
            from axiom.infra.ide import detect_ides, setup_ide

            ides = detect_ides()
            installed = [ide for ide in ides if ide.installed]

            if not installed:
                renderer.info("No supported IDEs detected — skipping.")
                return

            names = ", ".join(ide.name for ide in installed)
            renderer.text(f"Detected: {names}\n")

            # Collect schemas from project
            schemas = {}
            for schema_file in self.root.rglob("*-schema.json"):
                uri = schema_file.resolve().as_uri()
                stem = schema_file.stem.replace("-schema", "")
                schemas[uri] = f"{stem}.yaml"

            result = setup_ide(
                self.root,
                schemas=schemas,
                auto_install_extensions=True,
            )

            for config in result.get("configs_written", []):
                renderer.success(f"Configured: {config}")
            for ext in result.get("extensions_installed", []):
                renderer.success(f"Installed extension: {ext}")

            if not result.get("configs_written") and not result.get("extensions_installed"):
                renderer.info("IDEs already configured.")

        except ImportError:
            renderer.info("IDE module not available — skipping.")

    def _phase_done(self) -> None:
        renderer.heading("Setup Complete")
        renderer.blank()

        # Note about saved credentials
        env_path = self.root / ".env"
        if env_path.exists():
            renderer.success("Your connection settings are saved in .env")
            renderer.info("They load automatically every time you run an axi command.")
            renderer.blank()

        # Summary of results
        passed = sum(1 for v in self.state.test_results.values() if v == "pass")
        total = len(self.state.test_results)
        renderer.text(f"  {passed}/{total} connections working\n")

        # Show working connections
        channel_names = self.state.user_choices.get("_channel_names", {})
        for channel, status in self.state.test_results.items():
            name = channel_names.get(channel, channel.replace("_", " ").title())
            if status == "pass":
                renderer.success(name)
            elif status == "skip":
                renderer.info(f"{name} (not configured)")
            else:
                renderer.error(f"{name} (needs attention)")

        renderer.blank()

        # Offer to install the branded CLI shortcut command
        _cli_name = _get_branding().cli_name
        import shutil

        if not shutil.which(_cli_name):
            self._offer_shell_alias()

        cmd = _cli_name if shutil.which(_cli_name) else "python -m axiom.axiom_cli"

        renderer.heading("Next Steps")
        renderer.text("Try these commands:")
        renderer.text(f"  {cmd} doc status      — Check document lifecycle status")
        renderer.text(f"  {cmd} doc providers   — List available document providers")
        renderer.text(f"  {cmd} setup --status  — Review your configuration anytime")
        renderer.blank()

    # ------------------------------------------------------------------
    # Shell alias
    # ------------------------------------------------------------------

    def _offer_shell_alias(self) -> None:
        """Add a branded CLI alias to the user's shell config."""
        cli_name = _get_branding().cli_name

        # Find venv binary - check parent dir first (workspace layout), then local
        venv_bin = self.root.parent / ".venv" / "bin" / cli_name
        if not venv_bin.exists():
            venv_bin = self.root / ".venv" / "bin" / cli_name
        if not venv_bin.exists():
            # Fall back to hoping it's on PATH
            venv_bin = Path(cli_name)

        if platform.system() == "Windows":
            self._offer_powershell_alias(venv_bin)
            return

        shell = os.environ.get("SHELL", "")
        if "zsh" in shell:
            rc_file = Path.home() / ".zshrc"
            source_hint = "source ~/.zshrc"
        elif "bash" in shell:
            rc_file = Path.home() / ".bashrc"
            source_hint = "source ~/.bashrc"
        else:
            return  # Unknown shell, skip

        alias_line = f'alias {cli_name}="{venv_bin}"'
        duplicate_marker = f"alias {cli_name}="

        # Don't duplicate
        if rc_file.exists():
            content = rc_file.read_text(encoding="utf-8")
            if duplicate_marker in content:
                return

        with open(rc_file, "a", encoding="utf-8") as f:
            f.write(f"\n# {_get_cli_comment()}\n{alias_line}\n")

        renderer.success(f"Added '{cli_name}' shortcut to {rc_file.name}")
        renderer.info(f"Open a new terminal or run: {source_hint}")
        renderer.blank()

    def _offer_powershell_alias(self, venv_bin: Path) -> None:
        """Add a branded CLI function to the PowerShell profile."""
        cli_name = _get_branding().cli_name

        try:
            # Get PowerShell profile path
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "$PROFILE"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            profile_path = result.stdout.strip()
            if not profile_path:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return

        profile = Path(profile_path)
        # On Windows, venv binary is in Scripts\{cli_name}.exe
        if venv_bin.suffix != ".exe":
            venv_bin = venv_bin.parent.parent / "Scripts" / f"{cli_name}.exe"
        func_line = f'function {cli_name} {{ & "{venv_bin}" @args }}'

        # Don't duplicate
        if profile.exists():
            content = profile.read_text(encoding="utf-8")
            if f"function {cli_name}" in content:
                return

        profile.parent.mkdir(parents=True, exist_ok=True)
        with open(profile, "a", encoding="utf-8") as f:
            f.write(f"\n# {_get_cli_comment()}\n{func_line}\n")

        renderer.success(f"Added '{cli_name}' shortcut to PowerShell profile")
        renderer.info("Open a new PowerShell window to use it.")
        renderer.blank()

    # ------------------------------------------------------------------
    # Status display (non-interactive)
    # ------------------------------------------------------------------

    def _show_repo_sources(self) -> None:
        """Probe configured repo sources and display status."""
        renderer.heading("Repository Sources")
        try:
            from axiom.extensions.builtins.repo.config import detect_sources

            sources = detect_sources()
            if not sources:
                renderer.warning("No repo sources detected (set GITLAB_TOKEN or GITHUB_TOKEN)")
                return
            for source in sources:
                # Try to authenticate
                try:
                    from axiom.extensions.builtins.repo.orchestrator import _create_provider

                    provider = _create_provider(source)
                    ok = provider.authenticate()
                except Exception:
                    ok = False
                label = f"{source.provider.title()}  {source.group_or_org} ({source.token_env})"
                if ok:
                    renderer.success(label)
                else:
                    renderer.error(f"{label} — auth failed")
            renderer.text(f"\n  {len(sources)} repo source(s) detected")
        except Exception as exc:
            renderer.warning(f"Could not probe repo sources: {exc}")

    def show_status(self) -> None:
        """Display current configuration status without entering wizard."""
        renderer.heading(f"{_get_product_name()} Configuration Status")

        probe = run_probe(self.root)

        # Connection settings
        renderer.heading("Connection Settings")
        for var, is_set in probe.env_vars_set.items():
            name = renderer.friendly_name(var)
            if is_set:
                renderer.success(f"{name} — configured")
            else:
                renderer.warning(f"{name} — not set")

        # Repo sources
        self._show_repo_sources()

        # Config files
        renderer.heading("Configuration Files")
        for path, exists in probe.config_files_exist.items():
            if exists:
                renderer.success(path)
            else:
                renderer.warning(f"{path} — missing")

        # Dependencies
        renderer.heading("Tools & Libraries")
        for dep in probe.dependencies:
            label = dep.purpose or dep.name
            if dep.found:
                ver = f" ({dep.version})" if dep.version else ""
                renderer.status_line(label, f"Found{ver}", True)
            else:
                tag = "required" if dep.required else "optional"
                renderer.status_line(label, f"Not found ({tag})", not dep.required)

        # RAG corpus health — answers "is the corpus actually populated"
        # alongside the connection presence rendered above.
        renderer.heading("RAG")
        try:
            rag_health = collect_rag_health(
                rag_root=self.root / "runtime",
                known_corpora=("rag-community", "rag-org", "rag-internal"),
            )
            render_rag_health(rag_health)
        except Exception:  # pragma: no cover — defensive belt-and-suspenders
            renderer.warning("RAG health unavailable")

        # Routing classifier health — answers "is the Stage-2 SLM model
        # actually pulled" so operators don't get silent over-blocking
        # when only the cloud endpoint is reachable.
        self._render_classifier_section()

        renderer.blank()

    def _render_classifier_section(self) -> None:
        """Render the Routing Classifier health block.

        Mirrors the RAG section: collect via the domain-agnostic helper,
        render via the lazy-Console renderer, and degrade tolerantly if
        anything raises.
        """
        try:
            health = collect_classifier_health()
            render_classifier_health(health)
        except Exception:  # pragma: no cover — defensive belt-and-suspenders
            renderer.warning("Routing classifier health unavailable")

    # ------------------------------------------------------------------
    # Fix a specific connection
    # ------------------------------------------------------------------

    def fix(self, name: str) -> None:
        """Reconfigure a specific connection by credential name."""
        # Normalize: accept both env var name and lowercase form
        lookup = name.upper().replace("-", "_")

        from axiom.setup.guides import get_guide

        guide = get_guide(lookup)
        if guide is None:
            # Try matching by lowercase
            for g in CREDENTIAL_GUIDES:
                if g.env_var.lower() == name.lower():
                    guide = g
                    break

        if guide is None:
            renderer.error(f"Unknown connection: {name}")
            renderer.text("Available connections:")
            for g in CREDENTIAL_GUIDES:
                renderer.text(f"  {g.env_var.lower()} — {g.display_name}")
            return

        renderer.heading(f"Reconfigure: {guide.display_name}")
        self._configure_credential(guide)
        renderer.success(f"Done. Run '{_get_branding().cli_name} config --status' to verify.")
