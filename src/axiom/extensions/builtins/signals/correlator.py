# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Entity resolution for axi signal.

Maps fuzzy mentions of people and initiatives to known entities loaded
from config/people.md and config/initiatives.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Config directory: tools/agents/config/ (gitignored, real data)
# Falls back to tools/agents/config.example/ if config/ doesn't exist
from axiom import REPO_ROOT as _REPO_ROOT

_RUNTIME_DIR = _REPO_ROOT / "runtime"
CONFIG_DIR = _RUNTIME_DIR / "config"
CONFIG_EXAMPLE_DIR = _RUNTIME_DIR / "config.example"


@dataclass
class Person:
    name: str
    aliases: list[str] = field(default_factory=list)  # Nicknames, phonetic variants
    usernames: dict[str, str] = field(default_factory=dict)  # platform → handle
    role: str = ""
    initiatives: list[str] = field(default_factory=list)
    # Derived: lowercase first name, last name, full name, aliases for matching
    _match_keys: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self):
        parts = self.name.split()
        keys = [self.name.lower()]
        if parts:
            keys.append(parts[0].lower())  # first name
            if len(parts) > 1:
                keys.append(parts[-1].lower())  # last name
        # All platform usernames become match keys
        for handle in self.usernames.values():
            if handle and handle != "—":
                for username in handle.split(","):
                    username = username.strip()
                    if username:
                        keys.append(username.lower())
        # Add all aliases as match keys (critical for STT correction)
        for alias in self.aliases:
            alias_lower = alias.lower().strip()
            if alias_lower and alias_lower not in keys:
                keys.append(alias_lower)
        self._match_keys = keys

    # Convenience accessors for common platforms
    @property
    def gitlab(self) -> str:
        return self.usernames.get("gitlab", "")

    @property
    def github(self) -> str:
        return self.usernames.get("github", "")


@dataclass
class Initiative:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    status: str = "Active"
    owners: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    strategic_weight: int = 3  # 1=low, 5=critical
    pause_reason: str = ""
    # Derived keywords for fuzzy matching
    _match_keys: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self):
        keys = [self.name.lower()]
        # Split name into words for partial matching
        for word in self.name.lower().split():
            if len(word) > 2:
                keys.append(word)
        # Add all aliases as match keys
        for alias in self.aliases:
            alias_lower = alias.lower().strip()
            if alias_lower and alias_lower not in keys:
                keys.append(alias_lower)
        # Add repo path fragments
        for repo in self.repos:
            keys.append(repo.lower().replace("*", "").strip("/"))
        self._match_keys = keys


class Correlator:
    """Loads people and initiatives from config, provides fuzzy matching."""

    def __init__(self, config_dir: Path | None = None):
        if config_dir is None:
            config_dir = CONFIG_DIR if CONFIG_DIR.exists() else CONFIG_EXAMPLE_DIR
        self.config_dir = config_dir
        self.people: list[Person] = []
        self.initiatives: list[Initiative] = []
        self._load()

    def _load(self):
        people_path = self.config_dir / "people.md"
        if people_path.exists():
            self.people = self._parse_people(people_path.read_text())

        init_path = self.config_dir / "initiatives.md"
        if init_path.exists():
            self.initiatives = self._parse_initiatives(init_path.read_text())

    @staticmethod
    def _parse_people(text: str) -> list[Person]:
        """Parse a markdown table of people using header-driven column lookup.

        Reads the header row to build a column map so any column order works
        and new columns (like GitHub) are picked up automatically.
        """
        people: list[Person] = []
        lines = text.strip().splitlines()
        col_map: dict[str, int] = {}

        # Normalised header name → internal field
        _HEADER_ALIASES: dict[str, str] = {
            "name": "name",
            "aliases": "aliases",
            "usernames": "usernames",
            # Legacy per-platform columns — merged into usernames dict
            "gitlab": "_legacy_gitlab",
            "github": "_legacy_github",
            "linear": "_legacy_linear",
            "role": "role",
            "initiative(s)": "initiatives",
            "initiatives": "initiatives",
        }

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if not cells or len(cells) < 2:
                continue

            # Separator row (e.g. |---|---|)
            if set(cells[0]) <= {"-", " ", ":"}:
                continue

            # Detect header row
            if not col_map and cells[0].lower() == "name":
                for idx, header in enumerate(cells):
                    key = header.lower().strip()
                    if key in _HEADER_ALIASES:
                        col_map[_HEADER_ALIASES[key]] = idx
                continue

            # If we never saw a header, fall back to positional defaults
            if not col_map:
                col_map = {"name": 0, "aliases": 1, "usernames": 2, "role": 3, "initiatives": 4}

            def _cell(field: str) -> str:
                idx = col_map.get(field)
                if idx is None or idx >= len(cells):
                    return ""
                return cells[idx]

            name = _cell("name")
            if not name or name == "Name":
                continue

            aliases_str = _cell("aliases")
            aliases = [a.strip() for a in aliases_str.split(",") if a.strip() and a.strip() != "—"]

            # Build usernames dict from the Usernames column ("platform:handle" pairs)
            usernames: dict[str, str] = {}
            usernames_raw = _cell("usernames")
            if usernames_raw and usernames_raw != "—":
                for pair in usernames_raw.split(","):
                    pair = pair.strip()
                    if ":" in pair:
                        platform, handle = pair.split(":", 1)
                        handle = handle.strip()
                        if handle and handle != "—":
                            usernames[platform.strip().lower()] = handle

            # Merge legacy per-platform columns into usernames dict
            for legacy_field, platform in (
                ("_legacy_gitlab", "gitlab"),
                ("_legacy_github", "github"),
                ("_legacy_linear", "linear"),
            ):
                val = _cell(legacy_field)
                if val and val != "—" and platform not in usernames:
                    usernames[platform] = val

            role = _cell("role")

            initiatives_str = _cell("initiatives")
            initiatives = [i.strip() for i in initiatives_str.split(",") if i.strip()]

            people.append(Person(
                name=name,
                aliases=aliases,
                usernames=usernames,
                role=role,
                initiatives=initiatives,
            ))
        return people

    @staticmethod
    def _parse_initiatives(text: str) -> list[Initiative]:
        """Parse a markdown table of initiatives using header-driven column lookup."""
        initiatives: list[Initiative] = []
        lines = text.strip().splitlines()
        col_map: dict[str, int] = {}

        _HEADER_ALIASES: dict[str, str] = {
            "id": "id",
            "name": "name",
            "aliases": "aliases",
            "status": "status",
            "owners": "owners",
            "repos": "repos",
            "gitlab repos": "repos",
            "linear project": "_linear",
            "weight": "weight",
            "pause reason": "pause_reason",
        }

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if not cells or len(cells) < 2:
                continue

            # Separator row
            if set(cells[0]) <= {"-", " ", ":"}:
                continue

            # Detect header row
            if not col_map and cells[0].lower() in ("id", "name"):
                for idx, header in enumerate(cells):
                    key = header.lower().strip()
                    if key in _HEADER_ALIASES:
                        col_map[_HEADER_ALIASES[key]] = idx
                continue

            # Positional fallback
            if not col_map:
                col_map = {"id": 0, "name": 1, "status": 2, "owners": 3, "repos": 4}

            def _cell(field_name: str) -> str:
                idx = col_map.get(field_name)
                if idx is None or idx >= len(cells):
                    return ""
                return cells[idx]

            init_id = _cell("id")
            name = _cell("name")
            if not name or name.lower() == "name":
                continue

            aliases_str = _cell("aliases")
            aliases = [a.strip() for a in aliases_str.split(",") if a.strip() and a.strip() != "—"]

            status = _cell("status") or "Active"
            owners_str = _cell("owners")
            repos_str = _cell("repos")
            owners = [o.strip() for o in owners_str.split(",") if o.strip()]
            repos = [r.strip() for r in repos_str.split(",") if r.strip()]

            weight_str = _cell("weight")
            try:
                strategic_weight = int(weight_str) if weight_str else 3
            except ValueError:
                strategic_weight = 3
            pause_reason = _cell("pause_reason")

            initiatives.append(Initiative(
                id=init_id,
                name=name,
                aliases=aliases,
                status=status,
                owners=owners,
                repos=repos,
                strategic_weight=strategic_weight,
                pause_reason=pause_reason,
            ))
        return initiatives

    def match_person(self, mention: str) -> Person | None:
        """Fuzzy-match a text mention to a known person.

        Handles partial names like "Cole" → "Cole Gentry",
        GitLab usernames like "cgentry7" → "Cole Gentry", etc.
        """
        mention_lower = mention.lower().strip()
        if not mention_lower:
            return None

        # Exact match on any key
        for person in self.people:
            if mention_lower in person._match_keys:
                return person

        # Substring match (e.g., "ondrej" in "ondrej chvala")
        for person in self.people:
            for key in person._match_keys:
                if mention_lower in key or key in mention_lower:
                    return person

        return None

    def match_initiative(self, mention: str) -> Initiative | None:
        """Fuzzy-match a topic mention to a known initiative.

        Matching order (first match wins):
        1. Exact name match (case-insensitive)
        2. Exact alias match (case-insensitive)
        3. Mention is a substring of initiative name
        4. Mention is a substring of any match key (alias, keyword, repo)
        5. Any multi-word alias is a substring of the mention
           (catches "Enhanced axi CLI PRD" via alias "axi CLI")
        """
        mention_lower = mention.lower().strip()
        if not mention_lower:
            return None

        # 1. Exact name match
        for init in self.initiatives:
            if mention_lower == init.name.lower():
                return init

        # 2. Exact alias match
        for init in self.initiatives:
            for alias in init.aliases:
                if mention_lower == alias.lower():
                    return init

        # 3. Mention is contained in initiative name
        for init in self.initiatives:
            if mention_lower in init.name.lower():
                return init

        # 4. Mention is contained in any match key
        for init in self.initiatives:
            for key in init._match_keys:
                if mention_lower in key:
                    return init

        # 5. Multi-word alias contained in mention (reverse substring)
        # Only for aliases with 2+ words to avoid false positives on short words
        best_match: Initiative | None = None
        best_len = 0
        for init in self.initiatives:
            for alias in init.aliases:
                alias_lower = alias.lower()
                if " " in alias_lower and alias_lower in mention_lower:
                    # Prefer the longest matching alias (most specific)
                    if len(alias_lower) > best_len:
                        best_match = init
                        best_len = len(alias_lower)
        if best_match:
            return best_match

        return None

    def resolve_people(self, names: list[str]) -> list[str]:
        """Resolve a list of name mentions to canonical names."""
        resolved = []
        for name in names:
            person = self.match_person(name)
            if person:
                resolved.append(person.name)
            else:
                resolved.append(name)
        return resolved

    def resolve_initiatives(self, topics: list[str]) -> list[str]:
        """Resolve a list of topic mentions to canonical initiative names.

        Deduplicates after resolution so that e.g. ["Project Alpha", "axi CLI"]
        becomes ["Project Alpha"] instead of ["Project Alpha", "Project Alpha"].
        """
        resolved: list[str] = []
        seen: set[str] = set()
        for topic in topics:
            init = self.match_initiative(topic)
            canonical = init.name if init else topic
            if canonical not in seen:
                resolved.append(canonical)
                seen.add(canonical)
        return resolved

    def resolve_signals(self, signals: list) -> list:
        """Resolve people and initiatives on a list of Signal objects in-place.

        Also updates detail text for commit_summary signals to use resolved names.
        """
        for signal in signals:
            old_people = signal.people.copy() if signal.people else []
            signal.people = self.resolve_people(signal.people)
            signal.initiatives = self.resolve_initiatives(signal.initiatives)

            # Update detail text for commit summaries to use resolved name
            if (signal.metadata.get("event") == "commit_summary"
                    and old_people and signal.people
                    and old_people[0] != signal.people[0]):
                signal.detail = signal.detail.replace(old_people[0], signal.people[0], 1)
                signal.raw_text = signal.raw_text.replace(old_people[0], signal.people[0], 1)
        return signals
