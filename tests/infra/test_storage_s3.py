# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for S3-compatible StorageProvider (SeaweedFS/AWS S3).

Uses moto to mock the S3 API in-process. TDD: written before implementation.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

BUCKET = "test-artifacts"


@pytest.fixture
def s3_storage():
    with mock_aws():
        # Create the bucket in the mock
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)

        from axiom.infra.storage.s3 import S3StorageProvider

        provider = S3StorageProvider(
            {
                "bucket": BUCKET,
                "region": "us-east-1",
            }
        )
        yield provider


@pytest.fixture
def sample_file(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_text("hello from S3 test")
    return f


class TestS3StorageProvider:
    def test_implements_abc(self):
        from axiom.infra.storage import StorageProvider
        from axiom.infra.storage.s3 import S3StorageProvider

        assert issubclass(S3StorageProvider, StorageProvider)

    def test_upload_download_roundtrip(self, s3_storage, sample_file, tmp_path):
        result = s3_storage.upload(sample_file, "models/test/sample.txt")
        assert result.success is True
        assert result.storage_id == "models/test/sample.txt"

        dest = tmp_path / "downloaded.txt"
        s3_storage.download("models/test/sample.txt", dest)
        assert dest.read_text() == "hello from S3 test"

    def test_list_with_prefix(self, s3_storage, sample_file):
        s3_storage.upload(sample_file, "models/a.txt")
        s3_storage.upload(sample_file, "models/b.txt")
        s3_storage.upload(sample_file, "other/c.txt")

        entries = s3_storage.list_artifacts("models/")
        names = {e.name for e in entries}
        assert names == {"a.txt", "b.txt"}

    def test_list_all(self, s3_storage, sample_file):
        s3_storage.upload(sample_file, "a.txt")
        s3_storage.upload(sample_file, "b.txt")
        entries = s3_storage.list_artifacts()
        assert len(entries) == 2

    def test_delete(self, s3_storage, sample_file, tmp_path):
        s3_storage.upload(sample_file, "to-delete.txt")
        assert s3_storage.delete("to-delete.txt") is True
        # Downloading deleted file should raise
        with pytest.raises(FileNotFoundError):
            s3_storage.download("to-delete.txt", tmp_path / "nope.txt")

    def test_move(self, s3_storage, sample_file, tmp_path):
        s3_storage.upload(sample_file, "old/file.txt")
        result = s3_storage.move("old/file.txt", "new/file.txt")
        assert result.storage_id == "new/file.txt"

        # Old key gone
        with pytest.raises(FileNotFoundError):
            s3_storage.download("old/file.txt", tmp_path / "nope.txt")

        # New key works
        dest = tmp_path / "moved.txt"
        s3_storage.download("new/file.txt", dest)
        assert dest.read_text() == "hello from S3 test"

    def test_config_missing_bucket_raises(self):
        from axiom.infra.storage.s3 import S3StorageProvider

        with mock_aws():
            with pytest.raises(ValueError, match="bucket"):
                S3StorageProvider({})

    def test_get_canonical_url(self, s3_storage, sample_file):
        s3_storage.upload(sample_file, "my/key.txt")
        url = s3_storage.get_canonical_url("my/key.txt")
        assert "test-artifacts" in url
        assert "my/key.txt" in url
