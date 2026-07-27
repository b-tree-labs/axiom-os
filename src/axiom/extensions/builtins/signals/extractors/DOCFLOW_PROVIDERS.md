# DocFlow Provider Architecture

## Overview

The DocFlow system uses a **plugin-based provider architecture** to support multiple document storage and sync backends. This enables seamless integration with MS 365, Google Workspace, Box, Dropbox, and future services without modifying core code.

## Provider Types

### 1. DocProvider (Single Documents)

For services where you track **individual documents** by URL/ID:

| Provider | Use Case | Document URIs |
|----------|----------|---------------|
| `google_docs` | Google Docs with comments | `https://docs.google.com/document/d/{id}` or just `{id}` |
| `box` | Box documents with comments | `https://app.box.com/file/{id}` or just `{id}` |
| `ms_graph` | MS 365 Word documents | SharePoint/OneDrive URLs or drive item IDs |

### 2. FolderSyncProvider (Folder Sync)

For services where you sync **entire folders** to `inbox/raw/docflow/{provider}/`:

| Provider | Use Case | Folder Path |
|----------|----------|-------------|
| `dropbox` | Dropbox folder sync | `/Neutron/Documents` |
| `google_drive` | Google Drive folder sync | Folder ID from URL |
| `onedrive` | OneDrive/SharePoint folder | Drive item ID or path |
| `box_folder` | Box folder sync | Folder ID |

## Conventions

### Folder Structure

```
inbox/raw/docflow/
├── google_drive/          # Files synced from Google Drive
│   ├── .sync_state.json   # Tracking state
│   ├── Project_PRD.docx
│   └── Budget.xlsx
├── dropbox/               # Files synced from Dropbox
│   ├── .sync_state.json
│   └── Meeting_Notes.docx
├── box/                   # Files synced from Box
└── local/                 # Manually placed files
    └── Review_Doc.docx
```

### Environment Variables

All credentials use the convention: `DOCFLOW_{PROVIDER}_{TYPE}`

```bash
# Google Docs / Drive
export DOCFLOW_GOOGLE_CLIENT_ID="xxx.apps.googleusercontent.com"
export DOCFLOW_GOOGLE_SECRET="GOCSPX-xxx"
export DOCFLOW_GOOGLE_REFRESH="1//xxx"
export DOCFLOW_GOOGLE_TOKEN="ya29.xxx"
export DOCFLOW_GOOGLE_DRIVE_FOLDER_ID="1abc123def456"

# Dropbox
export DOCFLOW_DROPBOX_TOKEN="sl.xxx"
export DOCFLOW_DROPBOX_FOLDER_PATH="/Neutron/Documents"

# Box
export DOCFLOW_BOX_TOKEN="xxx"

# MS Graph (Office 365)
export MS_GRAPH_TOKEN="eyJ0xxx"  # Legacy, also supports DOCFLOW_MS_GRAPH_TOKEN
```

### Config File (Optional)

`inbox/config/docflow_providers.yaml`:

```yaml
providers:
  google_drive:
    enabled: true
    folder_id: "1abc123def456"
    sync_extensions:
      - .docx
      - .xlsx
      - .pptx
      - .gdoc

  dropbox:
    enabled: true
    folder_path: "/Neutron/Documents"
    sync_extensions:
      - .docx
      - .xlsx
      - .pdf

  box:
    enabled: false  # Not configured

  local:
    enabled: true
    # Always processes inbox/raw/docflow/local/
```

## Adding a New Provider

### Step 1: Create Provider Class

```python
# In docflow_providers/__init__.py or separate file

from tools.extensions.builtins.sense.extractors.docflow_providers import (
    DocProvider,
    FolderSyncProvider,
    ProviderCapability,
    ProviderCredentials,
    ExternalChange,
    SyncedFile,
)

class MyCloudProvider(FolderSyncProvider):
    """Provider for MyCloud folder sync."""
    
    slug = "mycloud"
    display_name = "MyCloud Storage"
    capabilities = {
        ProviderCapability.FOLDER_LISTING,
        ProviderCapability.FETCH_CONTENT,
    }
    supported_extensions = (".docx", ".xlsx", ".pptx")
    
    def list_remote_files(self, folder_id=None) -> list[SyncedFile]:
        # Implement API calls
        ...
    
    def download_file(self, remote_id: str, local_path: Path) -> bool:
        # Implement download
        ...
```

### Step 2: Register Provider

```python
from tools.extensions.builtins.sense.extractors.docflow_providers import ProviderRegistry

ProviderRegistry.register_folder_provider("mycloud", MyCloudProvider)
```

### Step 3: Or Use Entry Points (for plugins)

In your plugin's `pyproject.toml`:

```toml
[project.entry-points."docflow.providers"]
mycloud = "mycloud_plugin:MyCloudProvider"
```

## Usage

### CLI Commands

```bash
# List available providers
neut signal providers

# Sync all configured folder providers
neut signal ingest --source docflow

# Sync specific provider
neut signal ingest --source docflow --provider dropbox
```

### Python API

```python
from tools.extensions.builtins.sense.extractors.docflow_providers import (
    ProviderRegistry,
    list_providers,
    sync_all_folders,
    fetch_doc,
)

# List configured providers
available = list_providers()
# {'doc': ['google_docs', 'box'], 'folder': ['dropbox', 'google_drive']}

# Sync all folder providers
changes = sync_all_folders()

# Fetch single document
content, comments = fetch_doc("google_docs", "1abc123...")
```

## Provider Capabilities

Each provider declares its capabilities:

| Capability | Description |
|------------|-------------|
| `FETCH_CONTENT` | Read document text content |
| `EXTRACT_COMMENTS` | Extract reviewer comments |
| `TRACKED_CHANGES` | Extract track changes (Word) |
| `REVISION_HISTORY` | Get version history |
| `FOLDER_LISTING` | List folder contents |
| `WATCH_CHANGES` | Real-time change webhooks |
| `PUSH_CONTENT` | Write content back |
| `CREATE_DOCUMENT` | Create new documents |

## Signal Flow

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────┐
│ Folder Provider │────▶│ inbox/raw/docflow│────▶│ extract()   │
│ (Dropbox, etc)  │ sync│ /{provider}/     │ read│ as signals  │
└─────────────────┘     └──────────────────┘     └─────────────┘
                                                        │
┌─────────────────┐                                     ▼
│ Doc Provider    │──────────────────────────────▶┌─────────────┐
│ (Google Docs)   │ direct fetch                  │ Signal      │
└─────────────────┘                               │ Queue       │
                                                  └─────────────┘
```

1. **Folder providers** sync files to `inbox/raw/docflow/{provider}/`
2. **Doc providers** fetch content directly via API
3. Both produce **signals** (comments, changes, content) for the suggest queue

## OAuth Setup Guides

### Google OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create OAuth 2.0 credentials (Desktop app type)
3. Enable Google Docs API and Google Drive API
4. Run OAuth flow to get refresh token
5. Set `DOCFLOW_GOOGLE_*` environment variables

### Dropbox OAuth

1. Go to [Dropbox App Console](https://www.dropbox.com/developers/apps)
2. Create app with full Dropbox access
3. Generate access token (or implement OAuth flow)
4. Set `DOCFLOW_DROPBOX_TOKEN`

### Box OAuth

1. Go to [Box Developer Console](https://app.box.com/developers/console)
2. Create new app with OAuth 2.0
3. Configure redirect URIs
4. Complete OAuth flow
5. Set `DOCFLOW_BOX_TOKEN`

## Troubleshooting

### Provider Not Available

```
Provider google_drive not configured
```

Check environment variables are set and credentials are valid.

### Sync State Issues

Delete `.sync_state.json` in the provider folder to force full resync:

```bash
rm inbox/raw/docflow/dropbox/.sync_state.json
neut signal ingest --source docflow
```

### Rate Limiting

Folder sync providers implement incremental sync to minimize API calls. If you hit rate limits:

1. Increase sync interval
2. Reduce files in synced folder
3. Use more specific folder paths
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
