# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SharePoint provider for Publisher.

Features:
- Interactive device-code auth via MSAL (requires `DOCFLOW_CLIENT_ID` and `DOCFLOW_TENANT_ID` or .neut/docflow/config.json)
- Download a shared document URL via Microsoft Graph `/shares/{shareId}/driveItem/content`
- Save .docx to `.neut/downloads/` and optionally convert to Markdown using `pandoc` if available

Usage:
    from axiom.extensions.builtins.publishing.providers.sharepoint import SharePointProvider
    p = SharePointProvider()
    p.pull(sharepoint_url, Path('docs/prds/mydoc.md'))

Notes:
- Install requirements: `pip install msal requests`
- Register an Azure AD app (public client) and set `DOCFLOW_CLIENT_ID` and `DOCFLOW_TENANT_ID` env vars
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

try:
    import msal
    from msal import SerializableTokenCache
except Exception:  # pragma: no cover - handled at runtime
    msal = None
    SerializableTokenCache = None  # type: ignore[assignment,misc]


CONFIG_PATH = Path('.neut/docflow/config.json')
TOKEN_CACHE_PATH = Path('.neut/docflow/token_cache.json')
DOWNLOADS_DIR = Path('.neut/downloads')

SCOPES = [
    'Files.ReadWrite.All',
    'offline_access',
    'User.Read'
]

GRAPH_BASE = 'https://graph.microsoft.com/v1.0'


def _load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    # fallback to env
    return {
        'client_id': os.getenv('DOCFLOW_CLIENT_ID'),
        'tenant_id': os.getenv('DOCFLOW_TENANT_ID'),
    }


@dataclass
class SharePointProvider:
    client_id: str | None = None
    tenant_id: str | None = None

    def __post_init__(self):
        cfg = _load_config()
        self.client_id = self.client_id or cfg.get('client_id')
        self.tenant_id = self.tenant_id or cfg.get('tenant_id')
        if msal is None:
            raise RuntimeError('msal package not installed. Run: pip install msal')
        if not self.client_id or not self.tenant_id:
            raise RuntimeError('Missing DOCFLOW_CLIENT_ID or DOCFLOW_TENANT_ID. Add to .neut/docflow/config.json or env vars.')
        assert msal is not None  # guarded above
        self.app = msal.PublicClientApplication(self.client_id, authority=f'https://login.microsoftonline.com/{self.tenant_id}')
        assert SerializableTokenCache is not None  # guarded by msal import
        self.token_cache = SerializableTokenCache()
        if TOKEN_CACHE_PATH.exists():
            try:
                self.token_cache.deserialize(TOKEN_CACHE_PATH.read_text())
            except Exception:
                # ignore corrupt cache
                pass
        self.app.token_cache = self.token_cache

    def _save_cache(self):
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE_PATH.write_text(self.token_cache.serialize())

    def _acquire_token_interactive(self):
        # Try silent first
        accounts = self.app.get_accounts()
        if accounts:
            result = self.app.acquire_token_silent(SCOPES, account=accounts[0])
            if result and 'access_token' in result:
                return result['access_token']
        # Device flow
        flow = self.app.initiate_device_flow(scopes=SCOPES)
        if 'user_code' not in flow:
            raise RuntimeError('Failed to create device flow: ' + json.dumps(flow))
        print(flow['message'])
        result = self.app.acquire_token_by_device_flow(flow)
        if 'access_token' in result:
            self._save_cache()
            return result['access_token']
        raise RuntimeError('Failed to acquire token: ' + json.dumps(result))

    @staticmethod
    def _make_share_id(url: str) -> str:
        # Graph "shares" API expects a url-safe base64 encoded string prefixed with 'u!'
        b = base64.urlsafe_b64encode(url.encode('utf-8')).decode('utf-8')
        b = b.rstrip('=')
        return 'u!' + b

    def _download_via_graph(self, share_url: str, dest: Path) -> Path:
        token = self._acquire_token_interactive()
        headers = {'Authorization': f'Bearer {token}'}
        share_id = self._make_share_id(share_url)
        # GET /shares/{share_id}/driveItem/content
        url = f'{GRAPH_BASE}/shares/{share_id}/driveItem/content'
        resp = requests.get(url, headers=headers, stream=True)
        if resp.status_code == 401:
            # try refreshing once
            token = self._acquire_token_interactive()
            headers['Authorization'] = f'Bearer {token}'
            resp = requests.get(url, headers=headers, stream=True)
        resp.raise_for_status()
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, 'wb') as fh:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    fh.write(chunk)
        return dest

    def pull(self, sharepoint_url: str, local_path: Path) -> Path:
        """Download the remote .docx and convert to Markdown if possible.

        Args:
            sharepoint_url: full SharePoint doc URL (as in registry)
            local_path: destination Markdown path to write
        Returns:
            Path to the written local file (Markdown if converted, else .docx saved path)
        """
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        # choose filename from doc id or from URL
        filename = sharepoint_url.split('file=')[-1].split('&')[0]
        filename = filename.replace('%20', ' ')
        docx_name = filename if filename.endswith('.docx') else (filename + '.docx')
        tmp_path = DOWNLOADS_DIR / docx_name
        print(f'Downloading remote doc to {tmp_path}...')
        self._download_via_graph(sharepoint_url, tmp_path)
        # Try converting with pandoc
        pandoc = shutil.which('pandoc')
        if pandoc:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            cmd = [pandoc, '-f', 'docx', '-t', 'gfm', '-o', str(local_path), str(tmp_path)]
            print('Converting .docx -> Markdown with pandoc...')
            subprocess.run(cmd, check=True)
            print(f'Written Markdown to {local_path}')
            return local_path
        else:
            # Save .docx next to local path and return path
            out = local_path.with_suffix('.docx')
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tmp_path, out)
            print(f'Pandoc not found; saved .docx to {out}. Install pandoc to convert to Markdown.')
            return out

    def push(self, local_path: Path, sharepoint_url: str) -> None:
        """Push local file to SharePoint by uploading to the drive item.

        This is a convenience: it replaces remote content with local file bytes.
        It expects the share URL to reference a file the caller can write to.
        """
        token = self._acquire_token_interactive()
        headers = {'Authorization': f'Bearer {token}'}
        share_id = self._make_share_id(sharepoint_url)
        # Get the driveItem id first
        meta_url = f'{GRAPH_BASE}/shares/{share_id}/driveItem'
        resp = requests.get(meta_url, headers=headers)
        resp.raise_for_status()
        item = resp.json()
        item_id = item.get('id')
        # Upload by path: /drive/items/{item-id}/content
        upload_url = f'{GRAPH_BASE}/drive/items/{item_id}/content'
        with open(local_path, 'rb') as fh:
            resp = requests.put(upload_url, headers=headers, data=fh)
        resp.raise_for_status()
        print(f'Pushed {local_path} -> {sharepoint_url}')
