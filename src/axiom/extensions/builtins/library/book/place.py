# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Where an item files in the Data Book, how sure that is, and why (ADR-213 D11).

Every item is filed automatically; nobody has to organize. Placement weighs four
kinds of evidence, in this precedence:

1. **Corrections.** A person moved items with the same signature (the same
   extractors, extension and key features) to a folder. Moves are feedback: the
   next similar item follows them.
2. **User folders.** A folder a person made extends the scheme. Its name and
   what is already in it are signals: an item whose name, text or features match
   the folder's name, or whose signature resembles the items already there,
   files into it. A person's own folder outranks the domain's category when it
   fits at least as well, because it is the person's statement of how they work.
3. **Scheme rules.** The domain's rules over extractor outputs and file facts.
4. **The legacy taxonomy.** The classifier chain's document type, mapped to a
   category of the same name when the scheme has one.

Below ``unsorted_below`` confidence the item goes to *Unsorted, needs a look*;
below ``review_below`` it is filed but marked for a look. Every placement keeps
the sentence that explains it ("Filed under Logs / Line B7 because ...").
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from axiom.extensions.builtins.library.book.scheme import BookScheme, Rule, fill_template

_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = {"the", "and", "for", "with", "from", "file", "files", "data", "new", "copy", "final", "draft"}


@dataclass
class Signals:
    """What the pipeline knows about an item when it files it."""

    name: str
    mime: str = ""
    extractors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    title: str = ""
    #: The legacy taxonomy's document type, when a classifier chain ran.
    document_type: str = ""

    @property
    def ext(self) -> str:
        return PurePosixPath(self.name.lower()).suffix

    def signature(self) -> set[str]:
        """Tokens that say what kind of item this is (not which one)."""
        sig = {f"x:{e}" for e in self.extractors}
        sig |= {f"tag:{t}" for t in self.tags}
        if self.ext:
            sig.add(f"ext:{self.ext}")
        for k, v in self.features.items():
            if isinstance(v, (str, int, bool)) and len(str(v)) <= 40:
                sig.add(f"f:{k}={str(v).lower()}")
        return sig

    def words(self) -> set[str]:
        hay = " ".join(
            [
                re.sub(r"[_.\-]+", " ", self.name.lower()),
                self.title.lower(),
                self.text[:4000].lower(),
                " ".join(str(v).lower() for v in self.features.values() if isinstance(v, (str, int))),
            ]
        )
        return {w for w in _WORD.findall(hay) if w not in _STOP}


@dataclass
class UserFolder:
    path: str
    label: str
    #: Signatures of the items already in it.
    member_signatures: list[set[str]] = field(default_factory=list)


@dataclass
class Correction:
    signature: set[str]
    to_path: str


@dataclass
class Candidate:
    path: str
    confidence: float
    source: str  # correction | user-folder | rule | taxonomy | unsorted
    because: str


@dataclass
class Placement:
    path: str
    labels: list[str]
    confidence: float
    source: str
    needs_look: bool
    because: str
    alternatives: list[Candidate] = field(default_factory=list)

    @property
    def explanation(self) -> str:
        where = " / ".join(self.labels)
        tail = " It needs a look." if self.needs_look and self.source != "unsorted" else ""
        return f"Filed under {where} because {self.because}.{tail}"

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "labels": list(self.labels),
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "needs_look": self.needs_look,
            "because": self.because,
            "explanation": self.explanation,
            "alternatives": [
                {"path": c.path, "confidence": round(c.confidence, 3), "source": c.source, "because": c.because}
                for c in self.alternatives
            ],
        }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _rule_matches(rule: Rule, s: Signals) -> bool:
    if rule.extractor and not any(fnmatch.fnmatch(e, p) for e in s.extractors for p in rule.extractor):
        return False
    if rule.tag and not set(rule.tag) & set(s.tags):
        return False
    if rule.ext and s.ext not in rule.ext:
        return False
    if rule.mime and not any(fnmatch.fnmatch(s.mime or "", m) for m in rule.mime):
        return False
    if rule.name and not any(fnmatch.fnmatch(s.name.lower(), p.lower()) for p in rule.name):
        return False
    for group, wanted in (("features", rule.feature), ("metadata", rule.metadata)):
        have = getattr(s, group)
        for k, v in wanted.items():
            got = have.get(k)
            if got in (None, ""):
                return False
            if v != "*" and str(got).lower() != str(v).lower():
                return False
    if rule.text_any and not any(re.search(p, s.text or "", re.I | re.M) for p in rule.text_any):
        return False
    return True


def _because(rule: Rule, s: Signals) -> str:
    try:
        return rule.because.format_map(_Fmt(s))
    except Exception:  # noqa: BLE001 - an explanation never fails a placement
        return rule.because


class _Fmt(dict):
    def __init__(self, s: Signals) -> None:
        super().__init__(name=s.name, ext=s.ext)
        self._s = s

    def __missing__(self, key: str) -> str:
        return str(self._s.features.get(key) or self._s.metadata.get(key) or key)


def _user_folder_candidate(folder: UserFolder, s: Signals, sig: set[str]) -> Candidate | None:
    folder_words = {w for w in _WORD.findall(re.sub(r"[_.\-/]+", " ", folder.label.lower())) if w not in _STOP}
    words = s.words()
    name_hit = len(folder_words & words) / len(folder_words) if folder_words else 0.0
    similar = max((_jaccard(sig, m) for m in folder.member_signatures), default=0.0)
    # Either signal alone can carry it: a folder named for the item, or a folder of items just like it.
    score = min(1.0, max(name_hit * 0.8, similar, 0.5 * name_hit + 0.6 * similar))
    if score < 0.4:
        return None
    reasons = []
    if name_hit:
        reasons.append(f"its name or contents mention {', '.join(sorted(folder_words & words))}")
    if similar >= 0.4:
        reasons.append(f"it resembles the {len(folder.member_signatures)} item(s) already in {folder.label!r}")
    return Candidate(
        path=folder.path,
        confidence=min(0.95, 0.45 + score * 0.5),
        source="user-folder",
        because=" and ".join(reasons) or f"it fits your folder {folder.label!r}",
    )


def place(
    s: Signals,
    scheme: BookScheme,
    *,
    user_folders: list[UserFolder] | None = None,
    corrections: list[Correction] | None = None,
) -> Placement:
    sig = s.signature()
    candidates: list[Candidate] = []

    # 1. Corrections: the strongest evidence is a person having moved this kind of item.
    votes: dict[str, list[float]] = {}
    for c in corrections or []:
        sim = _jaccard(sig, c.signature)
        if sim >= 0.6:
            votes.setdefault(c.to_path, []).append(sim)
    for path, sims in votes.items():
        conf = min(0.97, 0.7 + 0.1 * len(sims) + 0.15 * max(sims))
        n = len(sims)
        candidates.append(
            Candidate(path, conf, "correction", f"you moved {n} similar item{'s' if n != 1 else ''} here")
        )

    # 2. User folders.
    for folder in user_folders or []:
        cand = _user_folder_candidate(folder, s, sig)
        if cand is not None:
            candidates.append(cand)

    # 3. Scheme rules.
    signals = {"features": s.features, "metadata": s.metadata}
    for rule in sorted(scheme.rules, key=lambda r: -r.priority):
        if not _rule_matches(rule, s):
            continue
        path = fill_template(rule.category, signals)
        conf = rule.confidence
        if path is None:
            # A template value was missing: file under the static parent, less sure.
            path = rule.category.split("{", 1)[0].rstrip("/")
            conf = conf * 0.9
        candidates.append(Candidate(path, conf, "rule", _because(rule, s)))

    # 4. The legacy taxonomy's type, when the scheme has a category by that name.
    if s.document_type and s.document_type in scheme.by_path:
        candidates.append(
            Candidate(s.document_type, 0.5, "taxonomy", f"the classifier took it for a {s.document_type}")
        )

    rank = {"correction": 3, "user-folder": 2, "rule": 1, "taxonomy": 0}

    def key(c: Candidate) -> tuple[float, int]:
        # A user folder ties-or-beats a domain rule within 0.05 (precedence 2 over 3).
        bonus = 0.05 if c.source in ("correction", "user-folder") else 0.0
        return (c.confidence + bonus, rank.get(c.source, 0))

    candidates.sort(key=key, reverse=True)
    seen: set[str] = set()
    unique = [c for c in candidates if not (c.path in seen or seen.add(c.path))]
    if not unique or unique[0].confidence < scheme.unsorted_below:
        best = unique[0] if unique else None
        because = "nothing about it matched a category confidently"
        if best is not None:
            because += f" (closest: {' / '.join(scheme.labels_for(best.path))}, {best.confidence:.0%})"
        return Placement(
            path=scheme.unsorted,
            labels=scheme.labels_for(scheme.unsorted),
            confidence=best.confidence if best else 0.0,
            source="unsorted",
            needs_look=True,
            because=because,
            alternatives=unique[:3],
        )
    top = unique[0]
    return Placement(
        path=top.path,
        labels=scheme.labels_for(top.path),
        confidence=top.confidence,
        source=top.source,
        needs_look=top.confidence < scheme.review_below,
        because=top.because,
        alternatives=unique[1:4],
    )


__all__ = ["Candidate", "Correction", "Placement", "Signals", "UserFolder", "place"]
