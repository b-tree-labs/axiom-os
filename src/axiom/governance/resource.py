# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ResourceRef + ResourcePattern — typed references to resources.

Per spec-governance-fabric §1.2: resources have a typed reference; the
type carries enough information to compute the resource's classification
and to drive pattern-matching rules. Examples:

    ResourceRef.extension("expman")
    ResourceRef.fragment("memory://localhost/fragments/abc123")
    ResourceRef.channel("slack://team-rsc/#alerts")
    ResourceRef.endpoint("https://api.openai.com/v1/chat/completions")
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceRef:
    """A URI-shaped reference to a resource an action operates on."""

    scheme: str
    identifier: str
    """The part after ``scheme://``; opaque to the governance layer."""

    @classmethod
    def parse(cls, uri: str) -> ResourceRef:
        if not uri:
            raise ValueError("ResourceRef URI cannot be empty")
        if "://" not in uri:
            raise ValueError(f"ResourceRef must have a scheme: {uri!r}")
        scheme, _, identifier = uri.partition("://")
        if not scheme:
            raise ValueError(f"ResourceRef scheme cannot be empty: {uri!r}")
        return cls(scheme=scheme, identifier=identifier)

    @classmethod
    def extension(cls, name: str) -> ResourceRef:
        return cls(scheme="extension", identifier=name)

    @classmethod
    def fragment(cls, uri: str) -> ResourceRef:
        return cls.parse(uri)

    @classmethod
    def channel(cls, uri: str) -> ResourceRef:
        return cls.parse(uri)

    @classmethod
    def endpoint(cls, uri: str) -> ResourceRef:
        return cls.parse(uri)

    def __str__(self) -> str:
        return f"{self.scheme}://{self.identifier}"


@dataclass(frozen=True)
class ResourcePattern:
    """A pattern that matches one or many `ResourceRef`s.

    Supported forms:

    - ``"*"``                       — matches any resource.
    - ``"scheme://*"``              — matches any resource with that scheme.
    - ``"scheme://prefix*"``        — prefix match (trailing star). Lets a
      rule or capability scope to one mount of the composed HTTP app,
      whose resources are ``extension://<mount><path>``.
    - ``"scheme://identifier"``     — exact match.
    """

    value: str

    def matches(self, resource: ResourceRef) -> bool:
        if self.value == "*":
            return True
        if self.value.endswith("*"):
            # generalized prefix form; subsumes "scheme://*"
            return str(resource).startswith(self.value[:-1])
        return self.value == str(resource)


__all__ = ["ResourceRef", "ResourcePattern"]
