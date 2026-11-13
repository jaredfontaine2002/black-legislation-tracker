"""Rules-based stance v1 — the signal layer behind the Friendliness Score.

Every classification the taxonomy produces gets a direction tag so the
map can score states: ``supportive`` / ``harmful`` / ``mixed`` /
``unknown``. The method is deliberately the same philosophy as
taxonomy v1: signal terms (stance.json, inspectable without code)
matched against the bill's title + subjects + the matched terms of the
classification being tagged, and the exact terms that fired are carried
on the result as evidence a reviewer can audit or overturn.

Semantics (rules v1, deliberately simple):
* only supportive signals fired  -> ``supportive``
* only harmful signals fired     -> ``harmful``
* both fired                     -> ``mixed``
* neither fired                  -> ``unknown`` (never a guess)

Stance is computed PER CLASSIFICATION ROW, not per bill: the same bill
can be supportive in one category context and harmful in another, and
the map aggregates rows. There are no category->stance defaults on
purpose — a voting bill can cut either way, so the signals decide.

Two matching subtleties, both handled by span containment:
* "ban the box" (supportive) contains "ban" (harmful) — the shorter
  harmful match inside the longer supportive phrase is dropped.
* "prohibit discrimination" (supportive) works the same way if broader
  "prohibit ..." harmful phrases are ever added.
Bare "equity" is intentionally NOT a supportive signal: bills banning
diversity programs name "Diversity, Equity and Inclusion" in their
titles, so the bare word would mis-tag them. "health equity" and
"racial equity" are the supportive phrases instead.

This is a title/subject signal pass only — no bill full text, no model
judgement. A bill whose direction lives only in its text will read
``unknown`` here until the Phase 2 full-text pass lands.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from .taxonomy import Classification

SUPPORTIVE = "supportive"
HARMFUL = "harmful"
MIXED = "mixed"
UNKNOWN = "unknown"
LABELS = (SUPPORTIVE, HARMFUL, MIXED, UNKNOWN)

STANCE_FILE = Path(__file__).with_name("stance.json")


@dataclass(frozen=True)
class StanceRules:
    """The loaded stance.json (signal lists + version label)."""

    version: str
    label: str
    supportive_signals: tuple[str, ...]
    harmful_signals: tuple[str, ...]


@dataclass(frozen=True)
class Stance:
    """One rules-v1 stance verdict for a single classification row."""

    label: str = UNKNOWN
    supportive_terms: tuple[str, ...] = field(default_factory=tuple)
    harmful_terms: tuple[str, ...] = field(default_factory=tuple)
    rules_version: str = ""

    def terms_payload(self) -> dict[str, list[str]]:
        """Evidence as stored in bill_classifications.stance_terms."""
        return {
            "supportive": list(self.supportive_terms),
            "harmful": list(self.harmful_terms),
        }

    def terms_json(self) -> str:
        return json.dumps(self.terms_payload())


def load_stance_rules(path: Path | None = None) -> StanceRules:
    """Load signal lists from stance.json (editable without code)."""
    source = path or STANCE_FILE
    data: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    supportive = tuple(
        str(s).strip().lower() for s in data.get("supportive_signals", []) if str(s).strip()
    )
    harmful = tuple(
        str(s).strip().lower() for s in data.get("harmful_signals", []) if str(s).strip()
    )
    if not supportive or not harmful:
        raise ValueError(f"stance file {source} must define both signal lists")
    return StanceRules(
        version=str(data.get("version", "stance-v1")),
        label=str(data.get("label", "")),
        supportive_signals=supportive,
        harmful_signals=harmful,
    )


@dataclass(frozen=True)
class _Hit:
    polarity: str  # SUPPORTIVE or HARMFUL
    term: str
    text_index: int  # which input string the hit came from
    start: int
    end: int


def _find_hits(text: str, text_index: int, polarity: str, signals: Iterable[str]) -> list[_Hit]:
    hits: list[_Hit] = []
    for term in signals:
        pattern = r"\b" + re.escape(term) + r"\b"
        for match in re.finditer(pattern, text):
            hits.append(_Hit(polarity, term, text_index, match.start(), match.end()))
    return hits


def _drop_contained(hits: list[_Hit]) -> list[_Hit]:
    """Drop a hit whose span is strictly contained in a longer
    opposite-polarity hit in the same input string ("ban" inside
    "ban the box"). Same-polarity overlaps are harmless and kept."""
    kept: list[_Hit] = []
    for hit in hits:
        contained = any(
            other.polarity != hit.polarity
            and other.text_index == hit.text_index
            and other.start <= hit.start
            and hit.end <= other.end
            and (other.end - other.start) > (hit.end - hit.start)
            for other in hits
        )
        if not contained:
            kept.append(hit)
    return kept


def determine_stance(
    title: str = "",
    subjects: Iterable[Any] | None = None,
    matched_terms: Iterable[str] | None = None,
    *,
    rules: StanceRules | None = None,
) -> Stance:
    """Tag one classification row from its bill's title + subjects plus
    the classification's own matched terms (where available). Subjects
    may be plain strings or LegiScan subject dicts with a
    'subject_name' key, mirroring taxonomy.classify_bill."""
    loaded = rules or load_stance_rules()
    texts = [str(title or "").lower()]
    for subject in subjects or []:
        name = _subject_name(subject)
        if name:
            texts.append(name.lower())
    for term in matched_terms or []:
        if str(term).strip():
            texts.append(str(term).lower())

    hits: list[_Hit] = []
    for index, text in enumerate(texts):
        hits.extend(_find_hits(text, index, SUPPORTIVE, loaded.supportive_signals))
        hits.extend(_find_hits(text, index, HARMFUL, loaded.harmful_signals))
    hits = _drop_contained(hits)

    supportive_terms = tuple(
        dict.fromkeys(h.term for h in hits if h.polarity == SUPPORTIVE)
    )
    harmful_terms = tuple(dict.fromkeys(h.term for h in hits if h.polarity == HARMFUL))
    if supportive_terms and harmful_terms:
        label = MIXED
    elif supportive_terms:
        label = SUPPORTIVE
    elif harmful_terms:
        label = HARMFUL
    else:
        label = UNKNOWN
    return Stance(
        label=label,
        supportive_terms=supportive_terms,
        harmful_terms=harmful_terms,
        rules_version=loaded.version,
    )


def apply_stance(
    classifications: list[Classification],
    *,
    title: str = "",
    subjects: Iterable[Any] | None = None,
    rules: StanceRules | None = None,
) -> list[Classification]:
    """Return copies of the classifications with stance + stance_terms
    filled in. Each row is tagged from the bill's title/subjects plus
    THAT row's matched terms, so rows of the same bill can differ."""
    loaded = rules or load_stance_rules()
    out: list[Classification] = []
    for classification in classifications:
        stance = determine_stance(
            title=title,
            subjects=subjects,
            matched_terms=classification.matched_terms,
            rules=loaded,
        )
        out.append(
            replace(
                classification,
                stance=stance.label,
                stance_terms=stance.terms_json(),
            )
        )
    return out


def stance_legiscan_bill(
    bill: dict[str, Any],
    classification: Classification,
    *,
    rules: StanceRules | None = None,
) -> Stance:
    """Stance for one classification of a LegiScan getBill dict."""
    return determine_stance(
        title=str(bill.get("title", "")),
        subjects=bill.get("subjects") or [],
        matched_terms=classification.matched_terms,
        rules=rules,
    )


def _subject_name(subject: Any) -> str:
    if isinstance(subject, dict):
        return str(subject.get("subject_name") or subject.get("name") or "")
    return str(subject)
