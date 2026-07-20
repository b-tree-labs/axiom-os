# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Content sanitization — defense against prompt injection in federated content.

All content received via federation (model.yaml, research responses, facts,
material definitions) passes through sanitization before entering the
knowledge corpus or being used in LLM prompts.

Threat model:
- Malicious model.yaml with embedded prompt injection strings
- Research responses containing injection payloads
- Material descriptions designed to manipulate LLM behavior
- Facility pack manifests with hidden instructions
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class SanitizationResult:
    clean: bool
    original: str
    sanitized: str
    threats_found: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "threats_found": self.threats_found,
            "threat_count": len(self.threats_found),
        }


# Patterns that indicate prompt injection attempts
_INJECTION_PATTERNS = [
    # Direct instruction override
    (r"(?i)ignore\s+(all\s+)?previous\s+instructions", "instruction_override"),
    (r"(?i)disregard\s+(all\s+)?(prior|previous|above)", "instruction_override"),
    (r"(?i)forget\s+(everything|all)\s+(you|about)", "instruction_override"),
    (r"(?i)you\s+are\s+now\s+a", "role_hijack"),
    (r"(?i)act\s+as\s+(if|though)\s+you", "role_hijack"),
    (r"(?i)pretend\s+(to\s+be|you\s+are)", "role_hijack"),
    # System prompt extraction
    (r"(?i)repeat\s+(your|the)\s+(system|initial)\s+(prompt|instructions)", "prompt_extraction"),
    (r"(?i)what\s+(are|were)\s+your\s+(instructions|system\s+prompt)", "prompt_extraction"),
    (r"(?i)show\s+me\s+your\s+(system|initial)\s+prompt", "prompt_extraction"),
    # Data exfiltration
    (r"(?i)send\s+(this|the\s+following)\s+to\s+https?://", "data_exfil"),
    (r"(?i)fetch\s+https?://", "data_exfil"),
    (r"(?i)curl\s+", "command_injection"),
    (r"(?i)wget\s+", "command_injection"),
    # Code execution
    (r"(?i)exec\s*\(", "code_execution"),
    (r"(?i)eval\s*\(", "code_execution"),
    (r"(?i)import\s+os\b", "code_execution"),
    (r"(?i)subprocess\.", "code_execution"),
    (r"(?i)__import__", "code_execution"),
    # Delimiter injection (trying to break out of context)
    (r"```\s*system", "delimiter_injection"),
    (r"<\|im_start\|>system", "delimiter_injection"),
    (r"\[INST\]", "delimiter_injection"),
    (r"<\|system\|>", "delimiter_injection"),
    # Hidden text / invisible characters
    (r"[\u200b\u200c\u200d\ufeff]", "invisible_chars"),
    # Base64 encoded payloads (suspiciously long base64 in text fields)
    (r"[A-Za-z0-9+/]{100,}={0,2}", "encoded_payload"),
]


def sanitize_text(text: str) -> SanitizationResult:
    """Scan text for prompt injection patterns.

    Returns a SanitizationResult with:
    - clean: True if no threats found
    - sanitized: text with threats neutralized (patterns replaced with [REDACTED])
    - threats_found: list of threat types detected
    """
    if not text:
        return SanitizationResult(clean=True, original="", sanitized="")

    threats = []
    sanitized = text

    for pattern, threat_type in _INJECTION_PATTERNS:
        matches = re.findall(pattern, sanitized)
        if matches:
            threats.append(threat_type)
            sanitized = re.sub(pattern, f"[REDACTED:{threat_type}]", sanitized)

    return SanitizationResult(
        clean=len(threats) == 0,
        original=text,
        sanitized=sanitized,
        threats_found=list(set(threats)),  # deduplicate
    )


def sanitize_dict(data: dict, fields: list[str] | None = None) -> dict:
    """Sanitize string fields in a dict (e.g., model.yaml data).

    Args:
        data: Dictionary to sanitize.
        fields: Specific fields to check. If None, checks all string values.

    Returns:
        Dict with sanitized values. Nested dicts are traversed.
    """
    result = {}

    for key, value in data.items():
        if fields and key not in fields:
            result[key] = value
            continue

        if isinstance(value, str):
            sr = sanitize_text(value)
            result[key] = sr.sanitized
        elif isinstance(value, dict):
            result[key] = sanitize_dict(value, fields=None)
        elif isinstance(value, list):
            result[key] = [
                sanitize_dict(item)
                if isinstance(item, dict)
                else sanitize_text(item).sanitized
                if isinstance(item, str)
                else item
                for item in value
            ]
        else:
            result[key] = value

    return result


def check_yaml_injection(yaml_content: str) -> SanitizationResult:
    """Check YAML content for injection before parsing.

    YAML-specific threats:
    - !!python/object constructor (arbitrary code execution)
    - !!python/module
    - Extremely long strings (DoS via memory)
    """
    threats = []
    sanitized = yaml_content

    # YAML-specific code execution via constructors
    yaml_threats = [
        (r"!!python/object", "yaml_code_execution"),
        (r"!!python/module", "yaml_code_execution"),
        (r"!!python/name", "yaml_code_execution"),
        (r"!!python/apply", "yaml_code_execution"),
    ]

    for pattern, threat_type in yaml_threats:
        if re.search(pattern, sanitized):
            threats.append(threat_type)
            sanitized = re.sub(pattern, f"# BLOCKED: {threat_type}", sanitized)

    # Check for prompt injection in string values
    text_result = sanitize_text(yaml_content)
    threats.extend(text_result.threats_found)

    return SanitizationResult(
        clean=len(threats) == 0,
        original=yaml_content,
        sanitized=sanitized,
        threats_found=list(set(threats)),
    )


def verify_pack_integrity(pack_path, expected_checksums: dict[str, str]) -> dict:
    """Verify .axiompack or .facilitypack file integrity.

    Supply chain defense: verify SHA256 checksums of all files in a pack
    against expected values (from a trusted source like a signed manifest).
    """
    import hashlib
    from pathlib import Path

    pack = Path(pack_path)
    if not pack.exists():
        return {"valid": False, "error": "Pack not found", "mismatches": []}

    mismatches = []
    verified = 0

    for rel_path, expected_hash in expected_checksums.items():
        file_path = pack / rel_path
        if not file_path.exists():
            mismatches.append({"file": rel_path, "error": "missing"})
            continue

        actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            mismatches.append(
                {
                    "file": rel_path,
                    "expected": expected_hash[:16],
                    "actual": actual_hash[:16],
                }
            )
        else:
            verified += 1

    # Check for unexpected files (not in checksums)
    if pack.is_dir():
        for f in pack.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(pack))
                if rel not in expected_checksums and rel != "SHA256SUMS":
                    mismatches.append({"file": rel, "error": "unexpected_file"})

    return {
        "valid": len(mismatches) == 0,
        "verified": verified,
        "total": len(expected_checksums),
        "mismatches": mismatches,
    }
