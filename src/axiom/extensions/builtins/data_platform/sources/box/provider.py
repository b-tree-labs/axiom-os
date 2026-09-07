# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``BoxSourceProvider`` — the Box kind's :class:`SourceKindProvider` impl.

Implements the four seams the platform calls into:

- :meth:`add_register_args` — Box's CLI flags for ``axi data register
  <name> box ...``.
- :meth:`params_from_args` — argparse → params dict shape.
- :meth:`validate` — kind-specific checks on the saved config.
- :meth:`construct` — build a live :class:`BoxIngestSource` from a
  config at runtime (Dagster sensor / PLINTH run-ingest).

Adding a new kind (GDrive, SharePoint, …) is the same four-method
class. The platform stays unchanged.
"""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

from axiom.infra.paths import get_user_state_dir

from ...agents.plinth.connectors import ConnectorConfig
from .api import BoxBrowserApiClient
from .session_api import BoxSessionApiClient
from .source import BoxIngestSource


class BoxSourceProvider:
    """Box `IngestSource` kind provider."""

    kind = "box"
    description = "Box folder (Playwright-SSO session; pull-oriented)"
    shape = "document"  # explicit for clarity; the default read by source_shape() (ADR-001)

    # ---- CLI ------------------------------------------------------------

    def add_register_args(self, subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--folder-id",
            required=True,
            help="Box folder id (numeric) — the corpus root the source walks",
        )
        subparser.add_argument(
            "--session-path",
            help="path to a captured Box state.json (will be base64-encoded into params)",
        )
        subparser.add_argument(
            "--session-state-b64",
            help="already-base64-encoded Box state.json; for non-interactive flows",
        )
        subparser.add_argument(
            "--session-dir",
            help="directory containing state.json (mounted in-cluster); "
                 "default $AXI_STATE/credentials/box/",
        )
        subparser.add_argument(
            "--jwt-secret-ref",
            help="SecretRef URL resolving to the Box server-auth config "
                 "(the developer-console 'Download as JSON' blob), e.g. "
                 "env://BOX_JWT_CONFIG or keep://example-host/box/jwt. Enables "
                 "unattended server auth (auto-refreshing tokens) — preferred "
                 "over the SSO session for scheduled refresh.",
        )

    def params_from_args(self, args: argparse.Namespace) -> dict[str, str]:
        params: dict[str, str] = {"folder_id": args.folder_id}
        if getattr(args, "jwt_secret_ref", None):
            params["jwt_secret_ref"] = args.jwt_secret_ref
        # session_state_b64 takes precedence; --session-path is a convenience
        # that reads + b64-encodes the file for us.
        if getattr(args, "session_state_b64", None):
            params["session_state_b64"] = args.session_state_b64
        elif getattr(args, "session_path", None):
            p = Path(args.session_path).expanduser()
            if not p.exists():
                raise ValueError(f"session-path not found: {p}")
            params["session_state_b64"] = base64.b64encode(p.read_bytes()).decode("ascii")
        if getattr(args, "session_dir", None):
            params["session_dir"] = str(Path(args.session_dir).expanduser())
        return params

    # ---- validation -----------------------------------------------------

    def url_for(self, config: ConnectorConfig, ref_id: str) -> str | None:
        """Canonical Box web URL for a file id — no re-walk needed.

        Lets the platform (e.g. the ADR-091 URL backfill) resolve a shareable
        link from a stored Box file id without fetching the file again.
        """
        from .source import box_web_url

        return box_web_url(ref_id)

    def validate(self, config: ConnectorConfig) -> list[str]:
        errors: list[str] = []
        if not config.params.get("folder_id"):
            errors.append("box connector requires params.folder_id")
        # Either a base64 session blob OR a session directory must be available
        # at construct time. We don't require either at register time (the
        # operator may capture the session after registration), but warn-flag
        # in `axi data diagnose` is a future enhancement.
        return errors

    # ---- runtime construction -------------------------------------------

    def construct(self, config: ConnectorConfig) -> BoxIngestSource:
        """Build a live :class:`BoxIngestSource` from a saved config.

        Reads the session state from (in priority order):
        1. ``params.session_state_b64`` (base64 inline; what the chart
           mounts via Secret)
        2. ``params.session_dir`` (filesystem directory)
        3. Default ``$AXI_STATE/credentials/box/``
        """
        folder_id = config.params.get("folder_id")
        if not folder_id:
            raise ValueError(f"connector {config.name!r}: missing params.folder_id")

        # JWT server auth (preferred for unattended/scheduled refresh): the
        # app config is resolved through the SecretStore by SecretRef — never
        # read as a bare value here — so the private key lives in a real
        # keystore backend, not in the connector TOML or a loose file.
        jwt_auth = self._resolve_jwt_auth(config)

        session_dir = self._resolve_session_dir(config)
        # Default: pure-Python session client (no Chromium needed). The
        # Playwright-backed BoxBrowserApiClient is only kept as an opt-in
        # for environments where pure-cookie replay doesn't work (e.g. if
        # Box adds CSRF token enforcement on the API path).
        if os.environ.get("AXI_BOX_USE_BROWSER_API", "").lower() in {"1", "true", "yes"}:
            api = BoxBrowserApiClient(session_dir=session_dir, headless=True)
        else:
            api = BoxSessionApiClient(session_dir=session_dir, jwt_auth=jwt_auth)
        return BoxIngestSource(name=config.name, folder_id=folder_id, api_client=api)

    def preflight(self, config: ConnectorConfig):
        """Live-verify auth + folder visibility; return actionable checks.

        Two checks a non-coder can act on:
        1. Authentication — can we mint a token / open the session?
        2. Folder access — can the (service) account actually see the
           target folder? The classic Box gotcha: JWT service accounts
           have their own empty file space, so the folder must be shared
           with them explicitly. We surface the service-account email to
           paste into Box's collaborator box.
        """
        from ..contracts import PreflightCheck, PreflightResult

        checks: list[PreflightCheck] = []
        folder_id = config.params.get("folder_id", "")

        try:
            jwt_auth = self._resolve_jwt_auth(config)
            session_dir = self._resolve_session_dir(config)
            api = BoxSessionApiClient(session_dir=session_dir, jwt_auth=jwt_auth)
        except Exception as exc:  # noqa: BLE001 — surface as a check, not a crash
            checks.append(PreflightCheck(
                name="Credentials", ok=False,
                message=f"Could not load Box credentials: {exc}",
                remediation="Re-paste the Box app config; it didn't parse.",
                actor="you",
            ))
            return PreflightResult(connector=config.name, kind="box", checks=checks)

        # --- check 1: authentication + identity -----------------------------
        account_login = ""
        try:
            me = api.get_json("/users/me") or {}
            account_login = me.get("login", "") or me.get("name", "")
            checks.append(PreflightCheck(
                name="Authentication", ok=True,
                message=f"Authenticated as {account_login or 'the Box account'}.",
            ))
        except Exception as exc:  # noqa: BLE001
            checks.append(PreflightCheck(
                name="Authentication", ok=False,
                message=f"Box rejected the credentials: {exc}",
                remediation="In the Box Admin Console, authorize the app by its "
                            "Client ID (Apps → your app → Authorize). JWT/CCG apps "
                            "are inert until an admin authorizes them.",
                actor="admin",
            ))
            return PreflightResult(connector=config.name, kind="box", checks=checks)

        # --- check 2: folder visibility -------------------------------------
        try:
            folder = api.get_json(f"/folders/{folder_id}") or {}
            name = folder.get("name", folder_id)
            count = (folder.get("item_collection") or {}).get("total_count", "?")
            checks.append(PreflightCheck(
                name="Folder access", ok=True,
                message=f"Can see '{name}' ({count} items).",
            ))
        except Exception as exc:  # noqa: BLE001
            denied = "403" in str(exc) or "404" in str(exc)
            checks.append(PreflightCheck(
                name="Folder access",
                ok=False,
                message=f"Authenticated, but can't see folder {folder_id}."
                        if denied else f"Folder check failed: {exc}",
                remediation=(
                    f"In Box, open this folder and share it with "
                    f"{account_login or 'the app service account'} as a Viewer."
                ) if denied else "Check the folder id is correct.",
                copy_value=account_login,
                actor="you",
            ))

        return PreflightResult(connector=config.name, kind="box", checks=checks)

    def _resolve_jwt_auth(self, config: ConnectorConfig):
        """Build a Box server-auth object from ``params.jwt_secret_ref``.

        The ref is resolved via the secrets extension (provider chosen by
        scheme: ``env://`` / ``openbao://`` today, ``keep://`` once KEEP
        ships a SecretStoreProvider). The resolved JSON blob's shape
        selects the auth flow — CCG (client_id/secret/enterprise_id, no
        keypair) or JWT (the keypair config). Returns ``None`` when no ref
        is configured — callers then fall back to the SSO session.

        (Name kept for back-compat; returns CCG or JWT auth, both duck-typed
        to ``authorization_header()``.)
        """
        ref_url = config.params.get("jwt_secret_ref")
        if not ref_url:
            return None
        import json

        from axiom.extensions.builtins.secrets import SecretRef, resolve

        from .ccg_auth import BoxCcgAuth, BoxCcgConfig

        with resolve(SecretRef.parse(ref_url)) as secret:
            blob = json.loads(secret.as_str())

        # Blob shape selects the auth flow (most-specific first):
        #   OAuth refresh-token (has refresh_token; no enterprise admin) →
        #   CCG (client_id/secret/enterprise_id) → JWT (keypair config).
        from .oauth_auth import BoxOAuthAuth, BoxOAuthConfig

        if BoxOAuthConfig.is_oauth_blob(blob):
            return BoxOAuthAuth(BoxOAuthConfig.from_dict(blob))

        if BoxCcgConfig.is_ccg_blob(blob):
            return BoxCcgAuth(BoxCcgConfig.from_dict(blob))

        from .jwt_auth import BoxJwtAuth, BoxJwtConfig

        return BoxJwtAuth(BoxJwtConfig.from_dict(blob))

    def _resolve_session_dir(self, config: ConnectorConfig) -> Path:
        b64 = config.params.get("session_state_b64")
        if b64:
            # Materialize the inline blob into a real file so the
            # BoxBrowserApiClient (which reads `state.json` from disk)
            # finds it. Lives at a per-connector path so multiple Box
            # connectors don't collide.
            session_dir = Path(
                config.params.get("session_dir")
                or get_user_state_dir() / "credentials" / "box" / config.name
            )
            session_dir.mkdir(parents=True, exist_ok=True)
            state_file = session_dir / "state.json"
            if not state_file.exists() or state_file.read_bytes() != base64.b64decode(b64):
                state_file.write_bytes(base64.b64decode(b64))
                os.chmod(state_file, 0o600)
            return session_dir

        if config.params.get("session_dir"):
            return Path(config.params["session_dir"]).expanduser()

        return get_user_state_dir() / "credentials" / "box"


__all__ = ["BoxSourceProvider"]
