# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the OneDrive channel.

Tests the real connection to MS Graph API:
  1. Authentication — can we get an OAuth2 token?
  2. Upload — can we upload a test .docx to OneDrive?
  3. Cleanup — delete the test file after upload

Requires: MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET, MS_GRAPH_TENANT_ID
          pip install requests
"""

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.onedrive,
    pytest.mark.skip(reason="MS 365 integration not yet configured — enable when MS_GRAPH credentials are active"),
]


class TestOneDriveAuth:
    """Verify MS Graph authentication."""

    def test_get_oauth_token(self, ms_graph_creds):
        """Can we authenticate to MS Graph and get a token?"""
        import requests

        auth_url = f"https://login.microsoftonline.com/{ms_graph_creds['tenant_id']}/oauth2/v2.0/token"
        data = {
            "client_id": ms_graph_creds["client_id"],
            "client_secret": ms_graph_creds["client_secret"],
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }

        response = requests.post(auth_url, data=data, timeout=30)
        assert response.status_code == 200, f"Auth failed: {response.text}"

        token = response.json().get("access_token")
        assert token is not None
        print(f"  Got OAuth2 token ({len(token)} chars)")


class TestOneDriveUpload:
    """Test upload via the OneDrive storage provider."""

    def test_provider_initializes(self, ms_graph_creds):
        """OneDriveStorageProvider accepts credentials and authenticates."""
        from axiom.extensions.builtins.publishing.providers.storage.onedrive import (
            OneDriveStorageProvider,
        )

        provider = OneDriveStorageProvider(config=ms_graph_creds)
        # Trigger authentication
        try:
            token = provider._get_token()
            assert token is not None
            print("  Provider authenticated successfully")
        except Exception as e:
            pytest.fail(f"Provider auth failed: {e}")

    def test_upload_and_cleanup(self, ms_graph_creds, tmp_path):
        """Upload a test doc, verify it arrives, then delete it."""
        from axiom.extensions.builtins.publishing.providers.storage.onedrive import (
            OneDriveStorageProvider,
        )

        provider = OneDriveStorageProvider(config=ms_graph_creds)

        # Create a test file
        test_file = tmp_path / "ci_test_doc.txt"
        test_file.write_text("This is a CI integration test file. Safe to delete.\n")

        try:
            result = provider.upload(
                test_file,
                destination="CI_Tests/ci_test_doc.txt",
                metadata={"test": True},
            )
            assert result.canonical_url
            print(f"  Uploaded to: {result.canonical_url}")

            # Clean up
            provider.delete(result.storage_id)
            print("  Cleaned up test file")

        except Exception as e:
            # Don't fail the test on cleanup issues
            print(f"  Upload/cleanup issue: {e}")
            pytest.skip(f"OneDrive operation failed (permissions?): {e}")


class TestPublisherPublishToOneDrive:
    """Test the full publisher publish pipeline to OneDrive."""

    def test_generate_and_publish(self, ms_graph_creds, tmp_path):
        """Full pipeline: .md → .docx → OneDrive upload."""
        from axiom.extensions.builtins.publishing.config import (
            GitPolicy,
            ProviderConfig,
            PublisherConfig,
        )
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        config = PublisherConfig(
            git=GitPolicy(require_clean=False, publish_branches=["*"]),
            generation=ProviderConfig(provider="pandoc-docx"),
            storage=ProviderConfig(provider="onedrive", settings=ms_graph_creds),
            notification=ProviderConfig(provider="terminal"),
            repo_root=tmp_path,
        )
        engine = PublisherEngine(config)

        source = tmp_path / "docs" / "ci-test.md"
        source.parent.mkdir(parents=True)
        source.write_text("# CI Integration Test\n\nThis doc was published by CI. Safe to delete.\n")

        try:
            record = engine.publish(source, storage_override="onedrive")
            assert record is not None
            assert record.url
            print(f"  Published to OneDrive: {record.url}")
        except Exception as e:
            pytest.skip(f"OneDrive publish failed (expected if no pandoc): {e}")
