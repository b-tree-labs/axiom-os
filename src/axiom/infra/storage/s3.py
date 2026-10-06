# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S3-compatible StorageProvider — works with AWS S3, SeaweedFS, or any S3 API.

Uses boto3 with configurable endpoint_url for SeaweedFS/on-premise deployments.
No MinIO SDK — pure S3 API via boto3.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import boto3

from axiom.infra.storage.base import StorageEntry, StorageProvider, UploadResult


class S3StorageProvider(StorageProvider):
    """S3-compatible object storage provider."""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.bucket = config.get("bucket", "")
        if not self.bucket:
            raise ValueError("S3StorageProvider requires 'bucket' in config")

        self.region = config.get("region", "us-east-1")
        self.endpoint_url = config.get("endpoint_url")  # None = AWS, set for SeaweedFS

        kwargs: dict[str, Any] = {"region_name": self.region}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if config.get("access_key"):
            kwargs["aws_access_key_id"] = config["access_key"]
            kwargs["aws_secret_access_key"] = config.get("secret_key", "")

        self._client = boto3.client("s3", **kwargs)

    def upload(
        self,
        local_path: Path,
        destination: str | None = None,
        metadata: dict | None = None,
    ) -> UploadResult:
        key = destination or local_path.name
        metadata = metadata or {}

        self._client.upload_file(str(local_path), self.bucket, key)

        return UploadResult(
            storage_id=key,
            canonical_url=self._build_url(key),
            version=metadata.get("version", "v1"),
            metadata={"bucket": self.bucket, "key": key},
        )

    def download(self, storage_id: str, local_path: Path) -> Path:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.download_file(self.bucket, storage_id, str(local_path))
        except self._client.exceptions.NoSuchKey:
            raise FileNotFoundError(f"Artifact not found: {storage_id}")
        except Exception as e:
            if "404" in str(e) or "NoSuchKey" in str(e) or "Not Found" in str(e):
                raise FileNotFoundError(f"Artifact not found: {storage_id}")
            raise
        return local_path

    def move(self, source: str, destination: str) -> UploadResult:
        self._client.copy_object(
            Bucket=self.bucket,
            CopySource={"Bucket": self.bucket, "Key": source},
            Key=destination,
        )
        self._client.delete_object(Bucket=self.bucket, Key=source)
        return UploadResult(
            storage_id=destination,
            canonical_url=self._build_url(destination),
        )

    def get_canonical_url(self, storage_id: str) -> str:
        return self._build_url(storage_id)

    def list_artifacts(self, folder: str = "") -> list[StorageEntry]:
        kwargs: dict[str, Any] = {"Bucket": self.bucket}
        if folder:
            kwargs["Prefix"] = folder

        response = self._client.list_objects_v2(**kwargs)
        entries = []
        for obj in response.get("Contents", []):
            key = obj["Key"]
            entries.append(
                StorageEntry(
                    storage_id=key,
                    name=key.rsplit("/", 1)[-1],
                    size_bytes=obj.get("Size", 0),
                    last_modified=obj.get("LastModified", ""),
                    url=self._build_url(key),
                )
            )
        return entries

    def delete(self, storage_id: str) -> bool:
        self._client.delete_object(Bucket=self.bucket, Key=storage_id)
        return True

    def _build_url(self, key: str) -> str:
        if self.endpoint_url:
            return f"{self.endpoint_url}/{self.bucket}/{key}"
        return f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{key}"
