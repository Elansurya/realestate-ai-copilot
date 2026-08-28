"""
backend/app/utils/search_engine.py

Query-parsing and suggestion-assembly utilities for the Global Search
module of the Enterprise Real Estate AI Copilot CRM.

This module handles everything the API layer needs *before* a search
reaches ``SearchService``:
    * Parsing "advanced filter" query-string parameters (comma-separated
      module lists, ISO-8601 date bounds, a JSON-encoded ``extra``
      filter blob) into a proper ``app.schemas.search.SearchFilter``.
    * Normalizing/tokenizing free-text query input.
    * Assembling ranked autocomplete suggestions from a pool of
      candidate strings (e.g. a user's recent/top search queries),
      delegating the actual scoring/ordering to
      ``app.utils.search_ranker``.

Like ``search_ranker.py``, this module raises ONLY the project's
domain exceptions (``app.core.exceptions``) -- never
``HTTPException`` -- so it stays usable from any layer (API, service,
CLI/background jobs) without pulling in a web-framework dependency.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Iterable, Optional

from app.core.exceptions import ValidationException
from app.models.search import SearchModule
from app.schemas.search import SearchFilter
from app.utils.search_ranker import rank_strings

__all__ = [
    "parse_modules_param",
    "parse_date_param",
    "build_filter_from_query_params",
    "normalize_text",
    "tokenize",
    "get_suggestions",
    "highlight_snippet",
]

#: Maximum number of modules a single "advanced filter" request may
#: restrict a search to, to keep the resulting fan-out bounded.
_MAX_MODULES_PER_REQUEST: int = len(SearchModule)

_WHITESPACE_PATTERN = re.compile(r"\s+")
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Advanced-filter query-parameter parsing
# ---------------------------------------------------------------------------
def parse_modules_param(raw: Optional[str]) -> Optional[list[SearchModule]]:
    """Parses a comma-separated module list from a query-string parameter.

    Args:
        raw: The raw query-string value, e.g. ``"customer,lead,task"``,
            or ``None``/blank if no module restriction was supplied.

    Returns:
        Optional[list[SearchModule]]: The parsed, order-preserving,
        de-duplicated list of modules, or ``None`` if ``raw`` is empty.

    Raises:
        ValidationException: If any comma-separated token is not a
            valid module name, or if more tokens are supplied than
            there are known modules.
    """
    if raw is None or not raw.strip():
        return None

    tokens = [token.strip().lower() for token in raw.split(",") if token.strip()]
    if len(tokens) > _MAX_MODULES_PER_REQUEST:
        raise ValidationException(
            f"Too many modules requested ({len(tokens)}); at most "
            f"{_MAX_MODULES_PER_REQUEST} are supported."
        )

    valid_values = {module.value for module in SearchModule}
    invalid = [token for token in tokens if token not in valid_values]
    if invalid:
        raise ValidationException(
            f"Unknown module(s): {', '.join(invalid)}. "
            f"Valid modules are: {sorted(valid_values)}."
        )

    seen: set[str] = set()
    modules: list[SearchModule] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        modules.append(SearchModule(token))
    return modules


def parse_date_param(raw: Optional[str], field_name: str) -> Optional[datetime]:
    """Parses an ISO-8601 date/datetime string from a query parameter.

    Args:
        raw: The raw query-string value, or ``None``/blank if omitted.
        field_name: The logical name of the field being parsed (used
            only to produce a clear error message).

    Returns:
        Optional[datetime]: The parsed datetime, or ``None`` if ``raw``
        is empty.

    Raises:
        ValidationException: If ``raw`` is not a valid ISO-8601 date or
            datetime string.
    """
    if raw is None or not raw.strip():
        return None

    value = raw.strip()
    try:
        # `datetime.fromisoformat` accepts both date-only ("2026-01-01")
        # and full datetime ("2026-01-01T00:00:00+00:00") forms.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationException(
            f"{field_name} must be a valid ISO-8601 date/datetime "
            f"string; got '{raw}'."
        ) from exc


def build_filter_from_query_params(
    *,
    modules: Optional[list[SearchModule]],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
    extra_json: Optional[str],
) -> Optional[SearchFilter]:
    """Assembles a `SearchFilter` from already-parsed query parameters.

    Args:
        modules: Pre-parsed module restriction, if any (see
            :func:`parse_modules_param`).
        date_from: Pre-parsed inclusive lower date bound, if any.
        date_to: Pre-parsed inclusive upper date bound, if any.
        extra_json: A raw JSON-encoded object string for
            module-specific filter criteria (e.g.
            ``'{"status": "pending"}'``), or ``None``.

    Returns:
        Optional[SearchFilter]: The assembled filter, or ``None`` if
        every input was empty (i.e. no filtering was requested).

    Raises:
        ValidationException: If `extra_json` is supplied but is not
            valid JSON, or does not decode to a JSON object.
    """
    extra: Optional[dict] = None
    if extra_json is not None and extra_json.strip():
        try:
            decoded = json.loads(extra_json)
        except json.JSONDecodeError as exc:
            raise ValidationException(
                "extra filter criteria must be valid JSON."
            ) from exc
        if not isinstance(decoded, dict):
            raise ValidationException(
                "extra filter criteria must decode to a JSON object."
            )
        extra = decoded

    if not modules and date_from is None and date_to is None and extra is None:
        return None

    # SearchFilter's own `_validate_date_range` model validator (see
    # app/schemas/search.py) enforces date_from <= date_to for us.
    return SearchFilter(
        modules=modules, date_from=date_from, date_to=date_to, extra=extra
    )


# ---------------------------------------------------------------------------
# Free-text normalization / tokenization
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """Collapses whitespace and trims a free-text string.

    Args:
        text: The raw text to normalize.

    Returns:
        str: The trimmed text with internal whitespace runs collapsed
        to single spaces.
    """
    return _WHITESPACE_PATTERN.sub(" ", text).strip()


def tokenize(text: str) -> list[str]:
    """Splits free text into lower-cased alphanumeric tokens.

    Args:
        text: The text to tokenize.

    Returns:
        list[str]: The extracted tokens, in order, lower-cased.
    """
    return [match.lower() for match in _TOKEN_PATTERN.findall(text)]


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------
def get_suggestions(
    prefix: str, candidates: Iterable[str], *, limit: int = 10
) -> list[str]:
    """Builds ranked autocomplete suggestions for a partial query.

    Args:
        prefix: The user's partial/in-progress query text.
        candidates: A pool of candidate strings to suggest from (e.g.
            a user's recent search queries plus tenant-wide top
            queries).
        limit: Maximum number of suggestions to return.

    Returns:
        list[str]: The ranked suggestions, best match first. Empty if
        `prefix` is blank or no candidates are relevant.
    """
    normalized_prefix = normalize_text(prefix)
    if not normalized_prefix:
        return []
    return rank_strings(candidates, normalized_prefix, limit=limit)


def highlight_snippet(text: str, query: str, *, tag: str = "mark") -> str:
    """Wraps the first case-insensitive match of `query` within `text`.

    Purely cosmetic helper for rendering suggestion/result snippets in
    a UI; has no effect on ranking or search execution.

    Args:
        text: The text to highlight within.
        query: The query substring to highlight.
        tag: The HTML tag name to wrap the match in.

    Returns:
        str: `text` with the first match (if any) wrapped in
        `<tag>...</tag>`. Returned unchanged if `query` is blank or
        not found.
    """
    needle = query.strip()
    if not needle:
        return text

    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return text

    start, end = match.span()
    return f"{text[:start]}<{tag}>{text[start:end]}</{tag}>{text[end:]}"