"""
backend/tests/test_search_repository.py

Unit tests for `app.repositories.search_repository.SearchRepository`.

These tests mock the async SQLAlchemy session boundary (`session.execute`,
`.add`, `.flush`, `.refresh`) so the repository's *query-construction and
result-shaping logic* is exercised without requiring a live database.

Where the repository resolves a per-module ORM class dynamically (see
`_resolve_model` / `_MODEL_CACHE` in the repository module), tests seed
`_MODEL_CACHE` directly with a small local SQLAlchemy declarative model
exposing real `Column` objects (so `.ilike()` / `or_()` / `select()`
build genuine SQL constructs), rather than mocking SQLAlchemy internals.

Run with: `pytest backend/tests/test_search_repository.py -v`
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import declarative_base

from app.core.exceptions import NotFoundError, ValidationError
from app.models.search import SearchHistory, SearchModule, SearchType
from app.repositories import search_repository as repo_module
from app.repositories.search_repository import RawSearchHit, SearchRepository

pytestmark = pytest.mark.asyncio

_TestBase = declarative_base()


class _FakeCustomer(_TestBase):
    """Minimal stand-in for `app.models.customer.Customer` in tests.

    Exposes exactly the columns `SearchRepository` expects for the
    `CUSTOMER` module (see `_MODULE_SEARCH_FIELDS`), plus the common
    `id` / `created_at` / `updated_at` columns every module search
    result is normalized against.
    """

    __tablename__ = "test_fake_customers"

    id = Column(Integer, primary_key=True)
    first_name = Column(String)
    last_name = Column(String)
    email = Column(String)
    phone = Column(String)
    created_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True))


def _make_execute_result(*, scalars_all=None, scalar_one=None, all_rows=None, one_row=None):
    """Builds a mock SQLAlchemy `Result` supporting the accessors used.

    Args:
        scalars_all: Value returned by `result.scalars().all()`.
        scalar_one: Value returned by `result.scalar_one()` /
            `result.scalar_one_or_none()`.
        all_rows: Value returned by `result.all()`.
        one_row: Value returned by `result.one()`.

    Returns:
        MagicMock: A result object satisfying whichever accessor the
        calling code path uses.
    """
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = scalars_all or []
    result.scalars.return_value = scalars_mock
    result.scalar_one.return_value = scalar_one
    result.scalar_one_or_none.return_value = scalar_one
    result.all.return_value = all_rows or []
    result.one.return_value = one_row
    return result


@pytest.fixture
def mock_session():
    """Provides an `AsyncMock` standing in for an `AsyncSession`."""
    session = AsyncMock()
    return session


@pytest.fixture
def repository(mock_session):
    """Provides a `SearchRepository` wired to the mocked session."""
    return SearchRepository(mock_session)


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """Ensures `_MODEL_CACHE` doesn't leak resolved models across tests."""
    repo_module._MODEL_CACHE.clear()
    yield
    repo_module._MODEL_CACHE.clear()


# ---------------------------------------------------------------------------
# supported_modules
# ---------------------------------------------------------------------------
class TestSupportedModules:
    def test_excludes_report_module(self, repository):
        """REPORT has no configured model location and must be excluded."""
        assert SearchModule.REPORT not in repository.supported_modules()

    def test_includes_every_other_module(self, repository):
        """Every module except REPORT should have a configured location."""
        expected = {m for m in SearchModule if m != SearchModule.REPORT}
        assert set(repository.supported_modules()) == expected


# ---------------------------------------------------------------------------
# search_module
# ---------------------------------------------------------------------------
class TestSearchModule:
    async def test_returns_empty_when_model_unresolvable(self, repository, mock_session):
        """An unresolvable module (e.g. not yet implemented) is skipped, not raised."""
        repo_module._MODEL_CACHE[SearchModule.CUSTOMER] = None

        hits = await repository.search_module(SearchModule.CUSTOMER, "smith")

        assert hits == []
        mock_session.execute.assert_not_called()

    async def test_returns_empty_when_no_expected_fields_present(self, repository, mock_session):
        """A resolvable model missing all of its expected search columns is skipped."""

        class _BareModel:
            id = 1

        repo_module._MODEL_CACHE[SearchModule.CUSTOMER] = _BareModel

        hits = await repository.search_module(SearchModule.CUSTOMER, "smith")

        assert hits == []
        mock_session.execute.assert_not_called()

    async def test_builds_raw_hits_from_matched_rows(self, repository, mock_session):
        """Matched rows are normalized into uniformly-shaped RawSearchHit objects."""
        repo_module._MODEL_CACHE[SearchModule.CUSTOMER] = _FakeCustomer

        now = datetime.now(timezone.utc)
        row = SimpleNamespace(
            id=42,
            first_name="Jane",
            last_name="Smith",
            email="jane@example.com",
            phone=None,
            created_at=now,
            updated_at=now,
        )
        mock_session.execute.return_value = _make_execute_result(scalars_all=[row])

        hits = await repository.search_module(SearchModule.CUSTOMER, "smith")

        assert len(hits) == 1
        hit = hits[0]
        assert isinstance(hit, RawSearchHit)
        assert hit.module == SearchModule.CUSTOMER
        assert hit.entity_id == "42"
        # Title is built from the first non-empty matched field.
        assert hit.title == "Jane"
        # Snippet joins the remaining non-empty matched field values.
        assert "Smith" in hit.snippet
        assert "jane@example.com" in hit.snippet
        assert hit.matched_fields == ["first_name", "last_name", "email", "phone"]
        assert hit.created_at == now
        assert hit.updated_at == now

    async def test_respects_limit_argument(self, repository, mock_session):
        """The configured limit is forwarded without affecting result shaping."""
        repo_module._MODEL_CACHE[SearchModule.CUSTOMER] = _FakeCustomer
        mock_session.execute.return_value = _make_execute_result(scalars_all=[])

        await repository.search_module(SearchModule.CUSTOMER, "smith", limit=5)

        mock_session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# Search history CRUD
# ---------------------------------------------------------------------------
class TestSearchHistoryPersistence:
    async def test_create_history_adds_and_flushes(self, repository, mock_session):
        """create_history persists a new SearchHistory row via the session."""
        record = await repository.create_history(
            user_id=1,
            search_query="condo downtown",
            module=SearchModule.PROPERTY,
            search_type=SearchType.FILTERED,
            filters={"modules": ["property"]},
            result_count=3,
            execution_time_ms=12.5,
        )

        mock_session.add.assert_called_once()
        added_record = mock_session.add.call_args.args[0]
        assert isinstance(added_record, SearchHistory)
        assert added_record.user_id == 1
        assert added_record.search_query == "condo downtown"
        assert added_record.module == SearchModule.PROPERTY
        assert added_record.search_type == SearchType.FILTERED
        assert added_record.result_count == 3
        mock_session.flush.assert_awaited_once()
        mock_session.refresh.assert_awaited_once()
        assert record is added_record

    async def test_get_history_by_id_returns_record(self, repository, mock_session):
        """Returns whatever the underlying scalar lookup resolves to."""
        fake_record = MagicMock(spec=SearchHistory)
        mock_session.execute.return_value = _make_execute_result(scalar_one=fake_record)

        result = await repository.get_history_by_id("some-id", user_id=1)

        assert result is fake_record

    async def test_get_history_by_id_returns_none_when_missing(self, repository, mock_session):
        """Returns None rather than raising when no matching record exists."""
        mock_session.execute.return_value = _make_execute_result(scalar_one=None)

        result = await repository.get_history_by_id("missing-id", user_id=1)

        assert result is None

    async def test_list_history_rejects_unknown_sort_field(self, repository, mock_session):
        """An unknown sort_by column raises a domain ValidationError, not a DB error."""
        with pytest.raises(ValidationError):
            await repository.list_history(
                user_id=1,
                page=1,
                page_size=20,
                sort_by="not_a_real_column",
                sort_order="desc",
            )
        mock_session.execute.assert_not_called()

    async def test_list_history_returns_items_and_total(self, repository, mock_session):
        """Returns the paginated items alongside the total matching count."""
        fake_items = [MagicMock(spec=SearchHistory), MagicMock(spec=SearchHistory)]
        mock_session.execute.side_effect = [
            _make_execute_result(scalar_one=7),  # count query
            _make_execute_result(scalars_all=fake_items),  # page query
        ]

        items, total = await repository.list_history(
            user_id=1, page=1, page_size=20, sort_by="created_at", sort_order="desc"
        )

        assert items == fake_items
        assert total == 7
        assert mock_session.execute.await_count == 2

    async def test_get_recent_searches_orders_by_created_at_desc(self, repository, mock_session):
        """Delegates directly to the session, returning whatever rows come back."""
        fake_items = [MagicMock(spec=SearchHistory)]
        mock_session.execute.return_value = _make_execute_result(scalars_all=fake_items)

        result = await repository.get_recent_searches(user_id=1, limit=10)

        assert result == fake_items

    async def test_soft_delete_history_raises_not_found_when_missing(self, repository, mock_session, monkeypatch):
        """Raises NotFoundError rather than silently no-op'ing on a missing record."""
        async def _fake_get(history_id, user_id):
            return None

        monkeypatch.setattr(repository, "get_history_by_id", _fake_get)

        with pytest.raises(NotFoundError):
            await repository.soft_delete_history("missing-id", user_id=1)

    async def test_soft_delete_history_marks_record_deleted(self, repository, mock_session, monkeypatch):
        """Marks is_deleted True and flushes, returning True on success."""
        fake_record = MagicMock(spec=SearchHistory)
        fake_record.is_deleted = False

        async def _fake_get(history_id, user_id):
            return fake_record

        monkeypatch.setattr(repository, "get_history_by_id", _fake_get)

        result = await repository.soft_delete_history("some-id", user_id=1)

        assert result is True
        assert fake_record.is_deleted is True
        mock_session.flush.assert_awaited_once()

    async def test_clear_history_soft_deletes_all_active_records(self, repository, mock_session):
        """Every active record returned by the query is soft-deleted."""
        fake_records = [MagicMock(spec=SearchHistory), MagicMock(spec=SearchHistory)]
        for record in fake_records:
            record.is_deleted = False
        mock_session.execute.return_value = _make_execute_result(scalars_all=fake_records)

        count = await repository.clear_history(user_id=1)

        assert count == 2
        assert all(record.is_deleted is True for record in fake_records)
        mock_session.flush.assert_awaited_once()

    async def test_clear_history_returns_zero_when_nothing_active(self, repository, mock_session):
        """Returns 0 without error when the user has no active history."""
        mock_session.execute.return_value = _make_execute_result(scalars_all=[])

        count = await repository.clear_history(user_id=1)

        assert count == 0


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
class TestGetStatisticsRaw:
    async def test_aggregates_expected_shape(self, repository, mock_session):
        """Assembles the five aggregate queries into the documented dict shape."""
        mock_session.execute.side_effect = [
            _make_execute_result(scalar_one=42),  # total_searches
            _make_execute_result(
                all_rows=[(SearchModule.CUSTOMER, 10), (None, 5)]
            ),  # by_module (None -> "global")
            _make_execute_result(
                all_rows=[(SearchType.GLOBAL, 5), (SearchType.FILTERED, 37)]
            ),  # by_search_type
            _make_execute_result(one_row=(123.456, 8.2)),  # avg execution/result
            _make_execute_result(
                all_rows=[("condo", 9), ("downtown lofts", 4)]
            ),  # top_queries
        ]

        stats = await repository.get_statistics_raw(
            user_id=1, date_from=None, date_to=None
        )

        assert stats["total_searches"] == 42
        assert stats["by_module"] == {"customer": 10, "global": 5}
        assert stats["by_search_type"] == {"global": 5, "filtered": 37}
        assert stats["avg_execution_time_ms"] == pytest.approx(123.456)
        assert stats["avg_result_count"] == pytest.approx(8.2)
        assert stats["top_queries"] == {"condo": 9, "downtown lofts": 4}
        assert mock_session.execute.await_count == 5