"""
backend/app/utils/search_ranker.py

Framework- and persistence-agnostic ranking utilities for the Global
Search module of the Enterprise Real Estate AI Copilot CRM.

Everything in this module is a pure function operating on plain
strings/sequences -- it has no knowledge of SQLAlchemy, FastAPI, or
the domain models, and issues no I/O. This keeps relevance scoring and
pagination math independently testable and reusable anywhere in the
API layer that needs to rank or page through search-shaped data (e.g.
the ``/search/suggestions`` endpoint in ``app/api/v1/search.py``, or
any future admin dashboard built on top of Global Search).

.. note::
    ``SearchService`` (``app/services/search_service.py``) implements
    its own copy of the hit-scoring logic for ranking full cross-module
    search results, since that method operates on the service's
    ``RawSearchHit`` dataclass rather than the plain strings/primitives
    this module deals with. The scoring *bands* are intentionally kept
    identical between the two so ranking behaves consistently across
    the whole module; if the weights below ever change, update both.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Sequence

__all__ = [
    "score_text_match",
    "score_hit",
    "rank_strings",
    "paginate",
]

# ---------------------------------------------------------------------------
# Relevance scoring bands (kept in sync with SearchService._score_hit)
# ---------------------------------------------------------------------------
_SCORE_EXACT_MATCH: float = 1.0
_SCORE_STARTSWITH: float = 0.9
_SCORE_CONTAINS: float = 0.7
_SCORE_SECONDARY_CONTAINS: float = 0.4
_SCORE_FALLBACK: float = 0.1


def score_text_match(candidate: str, query: str) -> float:
    """Scores how relevant a single candidate string is to a query.

    Used to rank plain-string candidates (e.g. autocomplete
    suggestions drawn from recent/top search queries) rather than
    full search-result hits.

    Scoring bands (highest wins):
        * Exact (case-insensitive) match -> 1.0
        * Candidate starts with the query -> 0.9
        * Candidate contains the query -> 0.7
        * Fallback for any other candidate -> 0.1

    Args:
        candidate: The candidate string being scored.
        query: The user's (already-trimmed) query text.

    Returns:
        float: A relevance score in the ``[0, 1]`` range.
    """
    needle = query.strip().lower()
    haystack = candidate.strip().lower()

    if not needle or not haystack:
        return _SCORE_FALLBACK
    if haystack == needle:
        return _SCORE_EXACT_MATCH
    if haystack.startswith(needle):
        return _SCORE_STARTSWITH
    if needle in haystack:
        return _SCORE_CONTAINS
    return _SCORE_FALLBACK


def score_hit(title: str, snippet: Optional[str], query: str) -> float:
    """Scores how relevant a title/snippet pair is to a query.

    Mirrors the banding used by ``SearchService`` for full
    cross-module search results, exposed here as a standalone,
    dependency-free function for any other part of the API layer that
    needs equivalent scoring without depending on the service layer.

    Args:
        title: The primary display text of the candidate result.
        snippet: Secondary/supporting text for the candidate result,
            if any.
        query: The user's (already-trimmed) query text.

    Returns:
        float: A relevance score in the ``[0, 1]`` range.
    """
    needle = query.strip().lower()
    title_lower = (title or "").strip().lower()
    snippet_lower = (snippet or "").strip().lower()

    if not needle:
        return _SCORE_FALLBACK
    if title_lower == needle:
        return _SCORE_EXACT_MATCH
    if title_lower.startswith(needle):
        return _SCORE_STARTSWITH
    if needle in title_lower:
        return _SCORE_CONTAINS
    if needle in snippet_lower:
        return _SCORE_SECONDARY_CONTAINS
    return _SCORE_FALLBACK


def rank_strings(
    candidates: Iterable[str], query: str, *, limit: int = 10
) -> list[str]:
    """Ranks and truncates a set of candidate strings against a query.

    Deduplicates candidates case-insensitively (keeping the first-seen
    casing), scores each remaining candidate with
    :func:`score_text_match`, and returns the top ``limit`` ordered by
    descending score, with a stable alphabetical tiebreaker.

    Args:
        candidates: The raw candidate strings (e.g. past search
            queries) to rank.
        query: The user's current (partial) query text.
        limit: Maximum number of ranked candidates to return.

    Returns:
        list[str]: The top-ranked, deduplicated candidates, best match
        first.
    """
    seen: dict[str, str] = {}
    for candidate in candidates:
        cleaned = candidate.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        seen.setdefault(key, cleaned)

    scored = [
        (original, score_text_match(original, query))
        for original in seen.values()
    ]
    scored.sort(key=lambda pair: (-pair[1], pair[0].lower()))
    return [candidate for candidate, _ in scored[:limit]]


def paginate(
    items: Sequence, page: int, page_size: int
) -> tuple[list, int, int]:
    """Slices an in-memory sequence into a single page of results.

    Generic helper for any endpoint that needs to page through a
    already-fetched, in-memory list (as opposed to pagination pushed
    down into a database query, which the repository layer already
    handles for ``search_history``).

    Args:
        items: The full, ordered sequence of items to page through.
        page: 1-indexed page number to retrieve.
        page_size: Number of items per page.

    Returns:
        tuple[list, int, int]: A 3-tuple of ``(page_items, total,
        total_pages)``.
    """
    total = len(items)
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    offset = (page - 1) * page_size
    page_items = list(items[offset : offset + page_size])
    return page_items, total, total_pages