# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Static credential guides for axi config.

Pre-written walkthrough text for each credential. No LLM needed —
all guidance is static until a user's first LLM key is validated,
at which point the wizard may offer chat-assisted mode.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class CredentialGuide:
    """Everything needed to walk a user through one credential."""

    env_var: str
    display_name: str
    description: str
    required: bool
    steps: list[str]
    url: str
    validator: Callable[[str], bool] = field(default=lambda v: len(v) > 0)

    def validate(self, value: str) -> bool:
        """Check if a value looks like a valid credential."""
        return self.validator(value)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _is_gitlab_token(v: str) -> bool:
    """GitLab personal access tokens start with glpat-."""
    return v.startswith("glpat-") and len(v) > 10


def _is_uuid(v: str) -> bool:
    """Check for UUID format (with or without dashes)."""
    stripped = v.replace("-", "")
    return bool(re.match(r"^[0-9a-fA-F]{32}$", stripped))


def _is_ms_secret(v: str) -> bool:
    """MS Graph secrets are non-empty strings of reasonable length."""
    return len(v) >= 8


def _is_anthropic_key(v: str) -> bool:
    """Anthropic keys start with sk-ant-."""
    return v.startswith("sk-ant-") and len(v) > 15


def _is_openai_key(v: str) -> bool:
    """OpenAI keys start with sk-."""
    return v.startswith("sk-") and len(v) > 15


def _is_github_token(v: str) -> bool:
    """GitHub personal access tokens start with ghp_ or github_pat_."""
    return (v.startswith("ghp_") or v.startswith("github_pat_")) and len(v) > 10


def _is_linear_key(v: str) -> bool:
    """Linear keys start with lin_api_."""
    return v.startswith("lin_api_") and len(v) > 12


# ---------------------------------------------------------------------------
# Credential definitions
# ---------------------------------------------------------------------------

# Order matters: LLM keys first so chat-assisted mode can unlock early.
CREDENTIAL_GUIDES: list[CredentialGuide] = [
    CredentialGuide(
        env_var="ANTHROPIC_API_KEY",
        display_name="Anthropic access key",
        description="Powers the AI assistant for document analysis and signal extraction.",
        required=False,
        steps=[
            "Go to the Anthropic Console",
            "Sign in or create an account",
            'Click "Create Key" and give it a name like "my-agent"',
            "Copy the key — it starts with sk-ant-",
        ],
        url="https://console.anthropic.com/settings/keys",
        validator=_is_anthropic_key,
    ),
    CredentialGuide(
        env_var="OPENAI_API_KEY",
        display_name="OpenAI access key",
        description="Alternative AI provider for document analysis and signal extraction.",
        required=False,
        steps=[
            "Go to the OpenAI Platform",
            "Sign in or create an account",
            'Navigate to API Keys and click "Create new secret key"',
            "Copy the key — it starts with sk-",
        ],
        url="https://platform.openai.com/api-keys",
        validator=_is_openai_key,
    ),
    # Git hosting — GitHub first (most common), then GitLab, both optional.
    # Users with other git hosts (Gitea, Bitbucket) can set tokens via env vars.
    CredentialGuide(
        env_var="GITHUB_TOKEN",
        display_name="GitHub access key",
        description="Lets the platform read your repositories and track activity.",
        required=False,
        steps=[
            (
                "Go to GitHub Settings → Developer settings"
                " → Personal access tokens → Fine-grained tokens"
            ),
            'Click "Generate new token" — name it something like "axiom"',
            "Under Resource owner, select your org",
            "Set Repository access to All repositories (or select specific ones)",
            (
                "Under Permissions → Repository, enable read-only for:"
                " Contents, Issues, Pull requests, Metadata"
            ),
            "Copy the token — it starts with github_pat_",
        ],
        url="https://github.com/settings/tokens?type=beta",
        validator=_is_github_token,
    ),
    CredentialGuide(
        env_var="GITLAB_TOKEN",
        display_name="GitLab access key",
        description="For teams using GitLab (self-hosted or gitlab.com).",
        required=False,
        steps=[
            "Go to your GitLab instance (set GITLAB_URL env var for self-hosted)",
            'Navigate to User Settings → Access Tokens',
            "Create a new token with read_api and read_repository scopes",
            "Copy the token — it starts with glpat-",
        ],
        url="https://gitlab.com/-/user_settings/personal_access_tokens",
        validator=_is_gitlab_token,
    ),
    CredentialGuide(
        env_var="MS_GRAPH_CLIENT_ID",
        display_name="Microsoft 365 app ID",
        description="Identifies your app for Microsoft 365 file sharing and document storage.",
        required=False,
        steps=[
            "Go to the Azure Portal",
            'Navigate to "App registrations" and create a new registration',
            "Copy the Application (client) ID from the overview page",
            "It looks like a UUID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        ],
        url="https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps",
        validator=_is_uuid,
    ),
    CredentialGuide(
        env_var="MS_GRAPH_CLIENT_SECRET",
        display_name="Microsoft 365 app secret",
        description="Authenticates your app with Microsoft 365 services.",
        required=False,
        steps=[
            "In the Azure Portal, open your app registration",
            'Go to "Certificates & secrets" → "New client secret"',
            "Give it a description and choose an expiration",
            "Copy the secret Value (not the Secret ID)",
        ],
        url="https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps",
        validator=_is_ms_secret,
    ),
    CredentialGuide(
        env_var="MS_GRAPH_TENANT_ID",
        display_name="Microsoft 365 tenant ID",
        description="Identifies your organization for Microsoft 365 connections.",
        required=False,
        steps=[
            "In the Azure Portal, open your app registration",
            "Copy the Directory (tenant) ID from the overview page",
            "It looks like a UUID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        ],
        url="https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps",
        validator=_is_uuid,
    ),
    CredentialGuide(
        env_var="LINEAR_API_KEY",
        display_name="Linear access key",
        description="Syncs project tasks and issues with your Linear workspace.",
        required=False,
        steps=[
            "Go to Linear Settings",
            'Navigate to "API" under your account settings',
            "Create a new personal API key",
            "Copy the key — it starts with lin_api_",
        ],
        url="https://linear.app/settings/api",
        validator=_is_linear_key,
    ),
]


def get_guide(env_var: str) -> CredentialGuide | None:
    """Look up a credential guide by environment variable name."""
    for guide in CREDENTIAL_GUIDES:
        if guide.env_var == env_var:
            return guide
    return None


def get_required_guides() -> list[CredentialGuide]:
    """Return only required credential guides."""
    return [g for g in CREDENTIAL_GUIDES if g.required]


def get_optional_guides() -> list[CredentialGuide]:
    """Return only optional credential guides."""
    return [g for g in CREDENTIAL_GUIDES if not g.required]


def get_llm_guides() -> list[CredentialGuide]:
    """Return LLM provider credential guides."""
    return [g for g in CREDENTIAL_GUIDES if g.env_var in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    )]
