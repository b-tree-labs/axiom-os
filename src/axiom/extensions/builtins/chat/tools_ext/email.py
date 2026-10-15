# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Email composition and sending tools for neut chat.

Allows the user to draft, preview, and send email without leaving the
terminal. Drafts are saved as markdown in tools/agents/drafts/approved/
and can be sent via the SMTP notification provider.

Tools:
    email_draft    — Compose an email draft (to, subject, body).
    email_preview  — Preview a draft as it would render in an email client.
    email_send     — Send a drafted email via SMTP.
    email_list     — List saved email drafts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.orchestrator.actions import ActionCategory

from ..tools import ToolDef

_DRAFTS_DIR = _REPO_ROOT / "runtime" / "drafts" / "approved"
_CONFIG_DIR = _REPO_ROOT / "runtime" / "config"


# ── tool definitions ─────────────────────────────────────────────────

TOOLS = [
    ToolDef(
        name="email_draft",
        description=(
            "Compose and save an email draft. The body should be markdown. "
            "The draft is saved locally for preview before sending."
        ),
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address(es), comma-separated.",
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line.",
                },
                "body": {
                    "type": "string",
                    "description": "Email body in markdown format.",
                },
                "cc": {
                    "type": "string",
                    "description": "CC recipients, comma-separated.",
                },
                "filename": {
                    "type": "string",
                    "description": "Override the draft filename (without extension).",
                },
            },
            "required": ["to", "subject", "body"],
        },
    ),
    ToolDef(
        name="email_preview",
        description="Preview a saved email draft. Returns the rendered content.",
        category=ActionCategory.READ,
        parameters={
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Draft filename (e.g., 'email_to_kevin_2026-03-04'). If omitted, shows most recent.",
                },
            },
        },
    ),
    ToolDef(
        name="email_send",
        description=(
            "Send a previously drafted email via SMTP. "
            "Requires SMTP to be configured in .publisher.yaml."
        ),
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Draft filename to send. If omitted, sends the most recent draft.",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to actually send. Safety gate.",
                },
            },
        },
    ),
    ToolDef(
        name="email_list",
        description="List all saved email drafts.",
        category=ActionCategory.READ,
        parameters={"type": "object", "properties": {}},
    ),
]


# ── helpers ──────────────────────────────────────────────────────────


def _parse_email_draft(path: Path) -> dict:
    """Parse an email draft .md file into structured fields."""
    text = path.read_text(encoding="utf-8")
    result: dict = {"body": "", "to": "", "subject": "", "cc": "", "path": str(path)}

    lines = text.split("\n")
    body_start = 0

    # Look for metadata in first few lines
    for i, line in enumerate(lines[:10]):
        stripped = line.strip()
        if stripped.lower().startswith("**to:**") or stripped.lower().startswith("to:"):
            # Split on first ":" after the field name, strip bold markers and whitespace
            val = stripped.split(":**", 1)[-1] if ":**" in stripped else stripped.split(":", 1)[1]
            result["to"] = val.strip().strip("*").strip()
        elif stripped.lower().startswith("**subject:**") or stripped.lower().startswith("subject:"):
            val = stripped.split(":**", 1)[-1] if ":**" in stripped else stripped.split(":", 1)[1]
            result["subject"] = val.strip().strip("*").strip()
        elif stripped.lower().startswith("**cc:**") or stripped.lower().startswith("cc:"):
            val = stripped.split(":**", 1)[-1] if ":**" in stripped else stripped.split(":", 1)[1]
            result["cc"] = val.strip().strip("*").strip()
        elif stripped == "---" or stripped == "" and i > 0:
            body_start = i + 1
            break

    # Everything after metadata is the body
    if body_start > 0:
        result["body"] = "\n".join(lines[body_start:]).strip()
    else:
        result["body"] = text.strip()

    return result


def _find_email_drafts() -> list[Path]:
    """Find email draft files in the approved directory."""
    if not _DRAFTS_DIR.is_dir():
        return []
    return sorted(
        list(_DRAFTS_DIR.glob("email_*.md")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )


def _resolve_recipient(name: str) -> str | None:
    """Try to resolve a name to an email address from people config."""
    people_file = _CONFIG_DIR / "people.md"
    if not people_file.exists():
        return None
    try:
        text = people_file.read_text(encoding="utf-8")
        for line in text.split("\n"):
            if "|" in line and name.lower() in line.lower():
                parts = [p.strip() for p in line.split("|")]
                # Look for email-like strings in the row
                for part in parts:
                    if "@" in part:
                        return part
    except Exception:
        pass
    return None


# ── tool execution ───────────────────────────────────────────────────


def execute(name: str, params: dict) -> dict:
    handlers = {
        "email_draft": _handle_draft,
        "email_preview": _handle_preview,
        "email_send": _handle_send,
        "email_list": _handle_list,
    }
    handler = handlers.get(name)
    if not handler:
        return {"error": f"Unknown email tool: {name}"}
    try:
        return handler(params)
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


def _handle_draft(params: dict) -> dict:
    to_addr = params.get("to", "")
    subject = params.get("subject", "")
    body = params.get("body", "")
    cc = params.get("cc", "")

    if not to_addr or not subject or not body:
        return {"error": "to, subject, and body are all required."}

    # Build the draft file
    ts = datetime.now(UTC).strftime("%Y-%m-%d")
    filename = params.get("filename")
    if not filename:
        # Derive from recipient
        recipient_slug = to_addr.split("@")[0].split(",")[0].strip().replace(" ", "_").lower()
        filename = f"email_to_{recipient_slug}_{ts}"

    content_parts = [
        f"**To:** {to_addr}",
        f"**Subject:** {subject}",
    ]
    if cc:
        content_parts.append(f"**CC:** {cc}")
    content_parts.append("")
    content_parts.append("---")
    content_parts.append("")
    content_parts.append(body)

    content = "\n".join(content_parts) + "\n"

    _DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _DRAFTS_DIR / f"{filename}.md"
    out_path.write_text(content, encoding="utf-8")

    return {
        "path": str(out_path),
        "filename": filename,
        "to": to_addr,
        "subject": subject,
        "message": f"Draft saved to {out_path.name}. Use email_preview to review or email_send to deliver.",
    }


def _handle_preview(params: dict) -> dict:
    filename = params.get("filename")

    if filename:
        # Try exact match
        path = _DRAFTS_DIR / f"{filename}.md"
        if not path.exists():
            path = _DRAFTS_DIR / filename
        if not path.exists():
            return {"error": f"Draft not found: {filename}"}
    else:
        drafts = _find_email_drafts()
        if not drafts:
            return {"error": "No email drafts found."}
        path = drafts[0]

    parsed = _parse_email_draft(path)

    return {
        "filename": path.stem,
        "to": parsed["to"],
        "subject": parsed["subject"],
        "cc": parsed.get("cc", ""),
        "body": parsed["body"],
        "path": str(path),
    }


def _handle_send(params: dict) -> dict:
    filename = params.get("filename")
    confirm = params.get("confirm", False)

    if not confirm:
        return {
            "error": "Set confirm=true to send. This is a safety gate.",
            "hint": "Use email_preview first to verify the draft looks correct.",
        }

    # Find the draft
    if filename:
        path = _DRAFTS_DIR / f"{filename}.md"
        if not path.exists():
            path = _DRAFTS_DIR / filename
        if not path.exists():
            return {"error": f"Draft not found: {filename}"}
    else:
        drafts = _find_email_drafts()
        if not drafts:
            return {"error": "No email drafts found."}
        path = drafts[0]

    parsed = _parse_email_draft(path)
    recipients = [r.strip() for r in parsed["to"].split(",") if r.strip()]

    if not recipients:
        return {"error": "No recipients found in draft."}

    # Check for actual email addresses
    if not all("@" in r for r in recipients):
        # Try resolving names
        resolved = []
        for r in recipients:
            if "@" in r:
                resolved.append(r)
            else:
                email = _resolve_recipient(r)
                if email:
                    resolved.append(email)
                else:
                    return {
                        "error": f"Cannot resolve '{r}' to an email address. Add emails to tools/agents/config/people.md."
                    }
        recipients = resolved

    # Try to load SMTP provider
    try:
        from axiom.extensions.builtins.publishing.providers.notification.smtp import (
            SMTPNotificationProvider,
        )

        # Try loading config
        config = _load_smtp_config()
        if not config.get("from_address"):
            return {
                "error": "SMTP not configured. Add smtp settings to .publisher.yaml.",
                "hint": "Required: from_address, smtp_host, smtp_port. Optional: smtp_user, smtp_password.",
            }
        provider = SMTPNotificationProvider(config)
        success = provider.send(
            recipients=recipients,
            subject=parsed["subject"],
            body=parsed["body"],
        )
        if success:
            return {
                "message": f"Email sent to {', '.join(recipients)}.",
                "subject": parsed["subject"],
                "draft_path": str(path),
            }
        else:
            return {"error": "SMTP send returned failure. Check server settings."}
    except ImportError:
        return {"error": "SMTP provider not available."}
    except Exception as e:
        return {"error": f"Send failed: {e}"}


def _handle_list(params: dict) -> dict:
    drafts = _find_email_drafts()
    if not drafts:
        return {"message": "No email drafts found.", "drafts": []}

    results = []
    for path in drafts:
        parsed = _parse_email_draft(path)
        results.append(
            {
                "filename": path.stem,
                "to": parsed.get("to", ""),
                "subject": parsed.get("subject", ""),
                "modified": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
            }
        )

    return {"drafts": results}


def _load_smtp_config() -> dict:
    """Load SMTP config from .publisher.yaml."""
    from axiom.infra.config_loader import load_yaml

    # Try YAML configs first
    for name in (".publisher.yaml", ".publisher.yml"):
        data = load_yaml(_REPO_ROOT / name)
        if data:
            return data.get("notification", {}).get("smtp", {})

    # Fallback: try JSON config
    config_path = _REPO_ROOT / ".publisher.json"
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data.get("notification", {}).get("smtp", {})
    return {}
