# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Image attachments for the chat surface.

A neutral ``ImageAttachment`` carries the path + base64-encoded bytes
+ media type. Provider-specific block conversion (Anthropic vs OpenAI)
happens via ``to_block_for(provider_kind)`` so the agent can build a
message in whichever format the active provider expects without the
gateway having to introspect.

Surface:

  - ``ImageAttachment.from_path(path)`` — load + sniff mime
  - ``att.to_block_for("anthropic" | "openai")`` — provider block
  - ``build_user_message(text, images, provider_kind)`` — assemble
    the user message dict (string content if no images, list-of-blocks
    if images present)
  - ``detect_provider_kind(provider)`` — endpoint URL → kind tag

Vision is supported on Anthropic Claude 3+ and OpenAI gpt-4o /
gpt-4-vision, plus local OpenAI-compatible servers (llama.cpp,
Ollama with vision-capable models). PDF support is out of scope here.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

ProviderKind = Literal["anthropic", "openai"]

_SUPPORTED_MIMES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _sniff_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED_MIMES:
        raise ValueError(
            f"unsupported image type {suffix!r} "
            f"(expected one of {sorted(_SUPPORTED_MIMES)})"
        )
    return _SUPPORTED_MIMES[suffix]


@dataclass(frozen=True)
class ImageAttachment:
    path: Path
    media_type: str
    b64_data: str

    @classmethod
    def from_path(cls, path: Path | str) -> ImageAttachment:
        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"image not found: {p}")
        if not p.is_file():
            raise ValueError(f"not a file: {p}")
        media_type = _sniff_media_type(p)
        encoded = base64.b64encode(p.read_bytes()).decode("ascii")
        return cls(path=p, media_type=media_type, b64_data=encoded)

    def to_block_for(self, provider_kind: str) -> dict[str, Any]:
        if provider_kind == "anthropic":
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": self.media_type,
                    "data": self.b64_data,
                },
            }
        if provider_kind == "openai":
            return {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{self.media_type};base64,{self.b64_data}",
                },
            }
        raise ValueError(
            f"unknown provider kind {provider_kind!r} (expected 'anthropic' or 'openai')"
        )


def build_user_message(
    text: str,
    images: list[ImageAttachment],
    provider_kind: str,
) -> dict[str, Any]:
    """Build a user message dict with optional image attachments.

    No images → string content (matches the existing single-string
    message shape, keeps back-compat with downstream code that expects
    strings). With images → list-of-content-blocks shape, with images
    placed before text per Anthropic vision best practice.
    """
    if not images:
        return {"role": "user", "content": text}
    content: list[dict[str, Any]] = [
        img.to_block_for(provider_kind) for img in images
    ]
    if text:
        if provider_kind == "anthropic":
            content.append({"type": "text", "text": text})
        else:  # openai
            content.append({"type": "text", "text": text})
    return {"role": "user", "content": content}


def detect_provider_kind(provider: Any) -> str:
    """Map a provider's endpoint URL to ``"anthropic"`` or ``"openai"``.

    Local OpenAI-compatible endpoints (Ollama on :11434, llama-server,
    llamafile) classify as ``openai`` since they speak the same wire
    protocol for image input.
    """
    endpoint = (getattr(provider, "endpoint", "") or "").lower()
    if "anthropic" in endpoint:
        return "anthropic"
    return "openai"


__all__ = [
    "ImageAttachment",
    "ProviderKind",
    "build_user_message",
    "detect_provider_kind",
]
