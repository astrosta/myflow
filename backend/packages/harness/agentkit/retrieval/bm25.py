"""Generic BM25 scoring over pre-tokenized documents."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Bm25Document:
    """One document's precomputed term counts and metadata."""

    key: str
    counts: Mapping[str, int]
    length: int
    head_tokens: frozenset[str]


def bm25_scores(
    documents: Sequence[Bm25Document],
    *,
    query_terms: Mapping[str, float],
    k1: float = 1.2,
    b: float = 0.75,
    name_weight: float = 3.0,
) -> dict[str, float]:
    """Score documents with BM25 and return only strictly positive matches.

    query_terms carries the query token weights; callers can raise a term's
    weight for a domain keyword. head_tokens is metadata (name / description)
    and receives name_weight when hit.
    """
    if not documents or not query_terms:
        return {}

    document_frequency: dict[str, int] = {}
    for document in documents:
        for term in query_terms:
            if term in document.counts:
                document_frequency[term] = document_frequency.get(term, 0) + 1

    total = len(documents)
    average_length = (sum(document.length for document in documents) / total) or 1.0
    scores: dict[str, float] = {}
    for document in documents:
        score = 0.0
        for term, weight in query_terms.items():
            frequency = document.counts.get(term, 0)
            if not frequency:
                continue
            df = document_frequency.get(term, 0)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            length_ratio = document.length / average_length
            saturation = frequency * (k1 + 1) / (
                frequency + k1 * (1 - b + b * length_ratio)
            )
            boost = name_weight if term in document.head_tokens else 1.0
            score += weight * idf * saturation * boost
        if score > 0:
            scores[document.key] = score
    return scores


__all__ = ["Bm25Document", "bm25_scores"]
