"""
backend/tests/test_search_service.py

Unit tests for `app.services.search_service.SearchService`.

The repository is fully mocked (`AsyncMock`) so these tests isolate the
service's own responsibilities: query validation/sanitization, module
resolution, relevance ranking/sorting, pagination bounds-checking,
search-history orchestration, and statistics window validation.

Run with: `pytest backend/tests/test_search_service.py -v`
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import BusinessRuleError, NotFoundError, ValidationError
from app.models.search import SearchModule, SearchType
from app.repositories.search_repository import RawSearchHit
from app.schemas.search import (
    SearchFilter,
    SearchPaginationParams,
    SearchRequest,
    SearchSortingParams,
)
from app.services.search_service import SearchService

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_repository():
    """Provides a fully-mocked SearchRepository double."""
    repo = AsyncMock()
    repo.supported_modules = MagicMock(
        return_value=tuple(m for m in SearchModule if m != SearchModule.REPORT)
    )
    return repo


@pytest.fixture
def service(mock_repository):
    """Provides a SearchService wired to the mocked repository."""
    return SearchService(mock_repository)


def _hit(module=SearchModule.CUSTOMER, title="Jane Smith", snippet=None, created_at=None):
    return RawSearchHit(
        module=module,
        entity_id="1",
        title=title,
        snippet=snippet,
        matched_fields=["first_name"],
        created_at=created_at,
        updated_at=created_at,
    )


# ---------------------------------------------------------------------------
# Query sanitization
# ---------------------------------------------------------------------------
class TestSanitizeQuery:
    def test_escapes_percent_and_underscore_wildcards(self):
        sanitized = SearchService._sanitize_query("100%_off")
        assert sanitized == r"100\%\_off"

    def test_escapes_backslash(self):
        sanitized = SearchService._sanitize_query("a\\b")
        assert sanitized == r"a\\b"

    def test_strips_surrounding_whitespace(self):
        assert SearchService._sanitize_query("  smith  ") == "smith"

    def test_raises_when_only_wildcard_characters_remain(self):
        with pytest.raises(ValidationError):
            SearchService._sanitize_query("%%__")


# ---------------------------------------------------------------------------
# Module resolution
# ---------------------------------------------------------------------------
class TestResolveTargetModules:
    def test_defaults_to_every_supported_module_when_unfiltered(self, service, mock_repository):
        modules = service._resolve_target_modules(None)
        assert set(modules) == set(mock_repository.supported_modules())

    def test_defaults_to_every_supported_module_when_filter_has_no_modules(self, service):
        modules = service._resolve_target_modules(SearchFilter())
        assert SearchModule.CUSTOMER in modules

    def test_returns_only_requested_modules_when_supported(self, service):
        modules = service._resolve_target_modules(
            SearchFilter(modules=[SearchModule.TASK])
        )
        assert modules == (SearchModule.TASK,)

    def test_raises_business_rule_error_for_unsupported_module(self, service, mock_repository):
        mock_repository.supported_modules.return_value = (SearchModule.TASK,)
        with pytest.raises(BusinessRuleError):
            service._resolve_target_modules(
                SearchFilter(modules=[SearchModule.REPORT])
            )


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------
class TestScoreHit:
    def test_exact_title_match_scores_highest(self):
        assert SearchService._score_hit(_hit(title="Smith"), "smith") == 1.0

    def test_title_startswith_scores_second(self):
        score = SearchService._score_hit(_hit(title="Smithsonian"), "smith")
        assert 0.85 <= score < 1.0

    def test_title_contains_scores_third(self):
        score = SearchService._score_hit(_hit(title="Jane Smith"), "smith")
        assert 0.6 <= score < 0.9

    def test_snippet_contains_scores_fourth(self):
        score = SearchService._score_hit(
            _hit(title="Jane Doe", snippet="alias: smith"), "smith"
        )
        assert 0.3 <= score < 0.6

    def test_no_match_scores_fallback(self):
        score = SearchService._score_hit(_hit(title="Jane Doe"), "smith")
        assert score < 0.3


# ---------------------------------------------------------------------------
# Ranking / sorting
# ---------------------------------------------------------------------------
class TestRankAndSort:
    def test_rejects_unsupported_sort_field(self, service):
        with pytest.raises(ValidationError):
            service._rank_and_sort(
                [_hit()],
                query="smith",
                sorting=SearchSortingParams(sort_by="not_supported", sort_order="desc"),
            )

    def test_orders_by_relevance_descending(self, service):
        exact = _hit(title="smith")
        partial = _hit(title="Jane Smith")
        ranked = service._rank_and_sort(
            [partial, exact],
            query="smith",
            sorting=SearchSortingParams(sort_by="relevance", sort_order="desc"),
        )
        assert ranked[0][0] is exact

    def test_orders_by_created_at(self, service):
        now = datetime.now(timezone.utc)
        older = _hit(title="a", created_at=now - timedelta(days=1))
        newer = _hit(title="b", created_at=now)
        ranked = service._rank_and_sort(
            [older, newer],
            query="a",
            sorting=SearchSortingParams(sort_by="created_at", sort_order="desc"),
        )
        assert ranked[0][0] is newer


# ---------------------------------------------------------------------------
# execute_search
# ---------------------------------------------------------------------------
class TestExecuteSearch:
    async def test_happy_path_returns_ranked_paginated_response(
        self, service, mock_repository
    ):
        async def _fake_search_module(module, query, **kwargs):
            if module == SearchModule.CUSTOMER:
                return [_hit(module=SearchModule.CUSTOMER, title="smith")]
            return []

        mock_repository.search_module.side_effect = _fake_search_module

        request = SearchRequest(
            query="smith",
            search_type=SearchType.GLOBAL,
            pagination=SearchPaginationParams(page=1, page_size=20),
        )

        response = await service.execute_search(user_id=1, request=request)

        assert response.total == 1
        assert response.results[0].module == SearchModule.CUSTOMER
        mock_repository.create_history.assert_awaited_once()

    async def test_raises_not_found_when_page_beyond_available_results(
        self, service, mock_repository
    ):
        mock_repository.search_module.return_value = [_hit()]

        request = SearchRequest(
            query="smith",
            search_type=SearchType.GLOBAL,
            pagination=SearchPaginationParams(page=99, page_size=20),
        )

        with pytest.raises(NotFoundError):
            await service.execute_search(user_id=1, request=request)

    async def test_filtered_search_only_queries_requested_modules(
        self, service, mock_repository
    ):
        mock_repository.search_module.return_value = []

        request = SearchRequest(
            query="smith",
            search_type=SearchType.FILTERED,
            filters=SearchFilter(modules=[SearchModule.TASK]),
        )

        await service.execute_search(user_id=1, request=request)

        called_modules = {
            call.args[0] for call in mock_repository.search_module.await_args_list
        }
        assert called_modules == {SearchModule.TASK}


# ---------------------------------------------------------------------------
# Search history
# ---------------------------------------------------------------------------
class TestSearchHistory:
    async def test_get_history_raises_not_found_when_missing(
        self, service, mock_repository
    ):
        mock_repository.get_history_by_id.return_value = None
        with pytest.raises(NotFoundError):
            await service.get_history(user_id=1, history_id="missing")

    async def test_list_history_raises_not_found_beyond_last_page(
        self, service, mock_repository
    ):
        mock_repository.list_history.return_value = ([], 0)
        # total == 0 should NOT raise (nothing to page through yet).
        result = await service.list_history(
            user_id=1,
            pagination=SearchPaginationParams(page=1, page_size=20),
            sorting=SearchSortingParams(),
        )
        assert result.total == 0

        mock_repository.list_history.return_value = ([], 5)
        with pytest.raises(NotFoundError):
            await service.list_history(
                user_id=1,
                pagination=SearchPaginationParams(page=99, page_size=20),
                sorting=SearchSortingParams(),
            )

    async def test_get_recent_searches_dedupes_case_insensitively(
        self, service, mock_repository
    ):
        now = datetime.now(timezone.utc)

        def history(query, idx):
            return SimpleNamespace(
                id=idx,
                user_id=1,
                search_query=query,
                module=None,
                search_type=SearchType.GLOBAL,
                filters=None,
                result_count=0,
                execution_time_ms=1.0,
                created_at=now,
            )

        mock_repository.get_recent_searches.return_value = [
            history("Downtown Condo", 1),
            history("downtown condo", 2),
            history("Waterfront Villa", 3),
        ]

        result = await service.get_recent_searches(user_id=1, limit=10)

        assert [item.search_query for item in result] == [
            "Downtown Condo",
            "Waterfront Villa",
        ]

    async def test_get_recent_searches_rejects_out_of_range_limit(self, service):
        with pytest.raises(ValidationError):
            await service.get_recent_searches(user_id=1, limit=0)
        with pytest.raises(ValidationError):
            await service.get_recent_searches(user_id=1, limit=51)

    async def test_delete_history_delegates_to_repository(self, service, mock_repository):
        await service.delete_history(user_id=1, history_id="abc")
        mock_repository.soft_delete_history.assert_awaited_once_with("abc", 1)

    async def test_clear_history_delegates_to_repository(self, service, mock_repository):
        mock_repository.clear_history.return_value = 3
        result = await service.clear_history(user_id=1)
        assert result == 3
        mock_repository.clear_history.assert_awaited_once_with(user_id=1)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
class TestGetStatistics:
    async def test_raises_when_date_from_after_date_to(self, service):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            await service.get_statistics(date_from=now, date_to=now - timedelta(days=1))

    async def test_raises_when_window_exceeds_maximum_span(self, service):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            await service.get_statistics(
                date_from=now - timedelta(days=400), date_to=now
            )

    async def test_returns_assembled_statistics_response(self, service, mock_repository):
        mock_repository.get_statistics_raw.return_value = {
            "total_searches": 10,
            "by_module": {"customer": 6, "global": 4},
            "by_search_type": {"global": 4, "filtered": 6},
            "avg_execution_time_ms": 42.0,
            "avg_result_count": 3.5,
            "top_queries": {"smith": 2},
        }

        stats = await service.get_statistics(user_id=1)

        assert stats.total_searches == 10
        assert stats.by_module == {"customer": 6, "global": 4}
        mock_repository.get_statistics_raw.assert_awaited_once()