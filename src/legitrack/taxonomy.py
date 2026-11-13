"""Rules-based classification v1 (SPEC section 6 Phase 1 item 5, taxonomy 6.2).

This is the rules layer only: keyword and subject matching against the 11
draft categories in taxonomy.json (SPEC 6.2, pending Jared's sign-off).
Every result is labelled method='rule' with the taxonomy version, and
carries the exact terms that fired so a human reviewer can audit or
overturn it. It does NOT do direction tags, plain-English summaries, or
any model pass -- those are Phase 2. A bill with no matches gets an empty
list, never a guessed category.

Confidence semantics (rules v1, deliberately simple and documented):
* a matched LegiScan/OpenStates subject:        0.90
* a keyword in the title:                        0.80
* a keyword only in the description:             0.65
* each additional distinct matched term adds 0.05, capped at 0.95.
Confidence here means "strength of the rule evidence", not a probability
that the classification is correct.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

METHOD = "rule"
TAXONOMY_FILE = Path(__file__).with_name("taxonomy.json")

_SUBJECT_CONFIDENCE = 0.90
_TITLE_CONFIDENCE = 0.80
_DESCRIPTION_CONFIDENCE = 0.65
_EXTRA_TERM_BONUS = 0.05
_MAX_CONFIDENCE = 0.95


@dataclass(frozen=True)
class Category:
    """One taxonomy category with its rule terms."""

    id: str
    name: str
    keywords: tuple[str, ...]
    subjects: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    """One rules-v1 classification of a bill into a category."""

    category_id: str
    category_name: str
    confidence: float
    method: str = METHOD
    taxonomy_version: str = ""
    matched_terms: tuple[str, ...] = field(default_factory=tuple)
    matched_subjects: tuple[str, ...] = field(default_factory=tuple)
    # Filled in by legitrack.stance during ingestion (apply_stance);
    # None / "" when the stance pass has not run for this row.
    stance: str | None = None  # supportive / harmful / mixed / unknown
    stance_terms: str = ""  # JSON: {"supportive": [...], "harmful": [...]}


@dataclass(frozen=True)
class Taxonomy:
    """The loaded taxonomy.json (categories + version label)."""

    version: str
    label: str
    categories: tuple[Category, ...]

    def category(self, category_id: str) -> Category | None:
        for cat in self.categories:
            if cat.id == category_id:
                return cat
        return None


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    """Load taxonomy data from taxonomy.json (editable without code)."""
    source = path or TAXONOMY_FILE
    data: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    categories = tuple(
        Category(
            id=str(row["id"]),
            name=str(row["name"]),
            keywords=tuple(str(k).strip().lower() for k in row.get("keywords", []) if str(k).strip()),
            subjects=tuple(str(s).strip() for s in row.get("subjects", []) if str(s).strip()),
        )
        for row in data["categories"]
    )
    if not categories:
        raise ValueError(f"taxonomy file {source} contained no categories")
    return Taxonomy(
        version=str(data.get("version", "rules-v1")),
        label=str(data.get("label", "")),
        categories=categories,
    )


def classify_bill(
    title: str = "",
    description: str = "",
    subjects: Iterable[str] | None = None,
    *,
    taxonomy: Taxonomy | None = None,
) -> list[Classification]:
    """Classify one bill with rules v1. Multi-label: a bill can match
    several categories. Returns matches sorted by confidence, strongest
    first. Subjects may be plain strings or LegiScan subject dicts with a
    'subject_name' key.
    """
    tax = taxonomy or load_taxonomy()
    subject_names = [_subject_name(s) for s in (subjects or [])]
    subject_names = [s for s in subject_names if s]
    title_text = (title or "").lower()
    desc_text = (description or "").lower()

    results: list[Classification] = []
    for cat in tax.categories:
        matched_subjects = tuple(
            s for s in subject_names if s.lower() in {c.lower() for c in cat.subjects}
        )
        title_hits = tuple(k for k in cat.keywords if _contains(title_text, k))
        desc_hits = tuple(
            k for k in cat.keywords if k not in title_hits and _contains(desc_text, k)
        )
        matched_terms = title_hits + desc_hits
        if not matched_subjects and not matched_terms:
            continue
        base = 0.0
        if matched_subjects:
            base = max(base, _SUBJECT_CONFIDENCE)
        if title_hits:
            base = max(base, _TITLE_CONFIDENCE)
        if desc_hits:
            base = max(base, _DESCRIPTION_CONFIDENCE)
        evidence_count = len(matched_terms) + len(matched_subjects)
        confidence = min(_MAX_CONFIDENCE, base + _EXTRA_TERM_BONUS * max(0, evidence_count - 1))
        results.append(
            Classification(
                category_id=cat.id,
                category_name=cat.name,
                confidence=round(confidence, 3),
                taxonomy_version=tax.version,
                matched_terms=matched_terms,
                matched_subjects=matched_subjects,
            )
        )
    results.sort(key=lambda c: (-c.confidence, c.category_id))
    return results


def classify_legiscan_bill(bill: dict[str, Any], *, taxonomy: Taxonomy | None = None) -> list[Classification]:
    """Classify a LegiScan getBill dict (title/description/subjects shape)."""
    return classify_bill(
        title=str(bill.get("title", "")),
        description=str(bill.get("description", "")),
        subjects=bill.get("subjects") or [],
        taxonomy=taxonomy,
    )


def _subject_name(subject: Any) -> str:
    if isinstance(subject, dict):
        return str(subject.get("subject_name") or subject.get("name") or "")
    return str(subject)


def _contains(text: str, phrase: str) -> bool:
    """Whole-phrase containment on word boundaries, so 'dei' does not fire
    inside other words and multi-word phrases match as a unit."""
    if not text or not phrase:
        return False
    pattern = r"\b" + re.escape(phrase.strip()) + r"\b"
    return re.search(pattern, text) is not None
