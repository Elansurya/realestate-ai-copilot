"""
backend/tests/test_search_api.py

API-layer tests for the Global Search module.

Scope:
    Exercises `app.api.v1.search.router` mounted as an isolated FastAPI
    router, verifying HTTP-level concerns: route wiring (paths,
    methods, status codes), RBAC enforcement for restricted modules
    and tenant-wide statistics (`app.api.v1.search._enforce_module_rbac`
    / the `/statistics?scope=all` branch), request/response schema
    shape, and translation of the domain exceptions this router's own
    docstring documents:

        NotFoundError      -> 404
        ValidationError    -> 422
        BusinessRuleError  -> 400
        ForbiddenError     -> 403

    `app.services.search_service.SearchService` is replaced for every
    test via the `get_search_service` FastAPI dependency (overridden
    with a fully-mocked `AsyncMock` instance), so no repository or
    database is touched. This isolates "does the router wire things up
    correctly" from "does the service/repository behave correctly"
    (covered by `test_search_service.py` and `test_search_repository.py`
    respectively).

Auth strategy:
    `app.core.security.get_current_user` (the dependency
    `app.api.v1.search` itself imports/assumes, per its module
    docstring) is overridden via FastAPI's `app.dependency_overrides`
    to return a configurable fake principal (a `SimpleNamespace` with
    `.id` and `.role`), so tests don't need a real JWT.

    Because a project-wide exception-to-HTTP-status translation layer
    is out of scope for the Global Search module, this test module
    registers minimal local exception handlers -- for exactly the four
    domain exception types `app.api.v1.search` imports from
    `app.core.exceptions` -- reproducing only the status-code mapping
    already documented in that router's own docstring/`responses=`
    metadata. No new behavior is introduced beyond what the router
    already assumes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import (
    BusinessRuleError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.search import SearchModule, SearchType
from app.schemas.search import (
    SearchHistoryListResponse,
    SearchHistoryResponse,
    SearchResponse,
    SearchStatisticsResponse,
)
from app.api.v1.search import get_search_service, router

pytestmark = pytest.mark.asyncio

API_PREFIX = "/api/v1/search"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _register_domain_exception_handlers(fastapi_app: FastAPI) -> None:
    """Registers the exact status-code mapping documented by the router.

    See `app/api/v1/search.py`'s module docstring: this router assumes
    a global exception handler translates these four domain exceptions
    into the stated HTTP status codes. That handler lives outside the
    Global Search module's scope, so a minimal local equivalent is
    registered here purely so these HTTP-level tests can observe the
    documented behavior.
    """

    def _make_handler(status_code: int):
        async def _handler(_: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=status_code, content={"detail": str(exc)})

        return _handler

    fastapi_app.add_exception_handler(NotFoundError, _make_handler(404))
    fastapi_app.add_exception_handler(ValidationError, _make_handler(422))
    fastapi_app.add_exception_handler(BusinessRuleError, _make_handler(400))
    fastapi_app.add_exception_handler(ForbiddenError, _make_handler(403))


@pytest.fixture
def fake_service():
    """Builds an `AsyncMock` standing in for `SearchService`'s public interface."""
    return AsyncMock(name="fake_search_service")


@pytest.fixture
def app(fake_service):
    """Builds a FastAPI app mounting only the Global Search router.

    `get_search_service` is overridden to always return `fake_service`,
    and `get_db` is overridden to a no-op since the (mocked) service
    never touches it directly in these tests.
    """
    fastapi_app = FastAPI()
    fastapi_app.include_router(router, prefix="/api/v1")
    _register_domain_exception_handlers(fastapi_app)
    fastapi_app.dependency_overrides[get_db] = lambda: None
    fastapi_app.dependency_overrides[get_search_service] = lambda: fake_service
    return fastapi_app


@pytest.fixture
def current_user_role():
    """Mutable holder for the role/id the fake `current_user` should carry."""
    return {"role": "agent", "id": 1}


@pytest.fixture
def client(app, current_user_role):
    """Builds an `AsyncClient` bound to `app`, with auth overridden."""

    def _override_current_user():
        return SimpleNamespace(
            id=current_user_role["id"], role=current_user_role["role"]
        )

    app.dependency_overrides[get_current_user] = _override_current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _set_role(current_user_role: dict, role: str, user_id: int = 1) -> None:
    current_user_role["role"] = role
    current_user_role["id"] = user_id


def _search_response(**overrides) -> SearchResponse:
    defaults = dict(
        query="smith",
        search_type=SearchType.GLOBAL,
        modules_searched=[SearchModule.CUSTOMER],
        results=[],
        total=0,
        pagination={"page": 1, "page_size": 20},
        sorting={"sort_by": "relevance", "sort_order": "desc"},
        total_pages=0,
        execution_time_ms=1.5,
    )
    defaults.update(overrides)
    return SearchResponse.model_validate(defaults)


def _history_response(**overrides) -> SearchHistoryResponse:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid.uuid4(),
        user_id=1,
        search_query="downtown condo",
        module=None,
        search_type=SearchType.GLOBAL,
        filters=None,
        result_count=0,
        execution_time_ms=0.0,
        is_deleted=False,
        deleted_at=None,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return SearchHistoryResponse.model_validate(defaults)


def _statistics_response(**overrides) -> SearchStatisticsResponse:
    defaults = dict(
        total_searches=0,
        by_module={},
        by_search_type={},
        avg_execution_time_ms=0.0,
        avg_result_count=0.0,
        top_queries={},
    )
    defaults.update(overrides)
    return SearchStatisticsResponse.model_validate(defaults)


# ---------------------------------------------------------------------------
# POST /search
# ---------------------------------------------------------------------------
class TestExecuteSearchEndpoint:
    async def test_returns_200_with_ranked_results(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.execute_search.return_value = _search_response(total=0)

        async with client as c:
            response = await c.post(API_PREFIX, json={"query": "smith"})

        assert response.status_code == 200
        assert response.json()["query"] == "smith"
        fake_service.execute_search.assert_awaited_once()

    async def test_returns_422_when_query_too_short(self, client, current_user_role):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.post(API_PREFIX, json={"query": "a"})

        assert response.status_code == 422

    async def test_blocks_restricted_module_for_non_elevated_role(
        self, client, current_user_role
    ):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.post(
                API_PREFIX,
                json={
                    "query": "invoice",
                    "search_type": "filtered",
                    "filters": {"modules": ["payment"]},
                },
            )

        assert response.status_code == 403

    async def test_allows_restricted_module_for_elevated_role(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "manager")
        fake_service.execute_search.return_value = _search_response(
            modules_searched=[SearchModule.PAYMENT]
        )

        async with client as c:
            response = await c.post(
                API_PREFIX,
                json={
                    "query": "invoice",
                    "search_type": "filtered",
                    "filters": {"modules": ["payment"]},
                },
            )

        assert response.status_code == 200

    async def test_returns_400_on_business_rule_error(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "manager")
        fake_service.execute_search.side_effect = BusinessRuleError(
            "Global Search does not currently support the following module(s): report."
        )

        async with client as c:
            response = await c.post(
                API_PREFIX,
                json={
                    "query": "smith",
                    "search_type": "filtered",
                    "filters": {"modules": ["report"]},
                },
            )

        assert response.status_code == 400

    async def test_returns_404_when_page_beyond_results(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.execute_search.side_effect = NotFoundError(
            "Page 99 does not exist; only 1 page(s) of results are available."
        )

        async with client as c:
            response = await c.post(
                API_PREFIX,
                json={"query": "smith", "pagination": {"page": 99, "page_size": 20}},
            )

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /search/advanced
# ---------------------------------------------------------------------------
class TestAdvancedSearchEndpoint:
    async def test_returns_200_for_basic_query(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.execute_search.return_value = _search_response()

        async with client as c:
            response = await c.get(f"{API_PREFIX}/advanced", params={"q": "smith"})

        assert response.status_code == 200
        fake_service.execute_search.assert_awaited_once()

    async def test_parses_comma_separated_modules(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "manager")
        fake_service.execute_search.return_value = _search_response(
            modules_searched=[SearchModule.CUSTOMER, SearchModule.LEAD]
        )

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/advanced",
                params={
                    "q": "smith",
                    "search_type": "filtered",
                    "modules": "customer,lead",
                },
            )

        assert response.status_code == 200
        request_arg = fake_service.execute_search.await_args.kwargs["request"]
        assert request_arg.filters.modules == [
            SearchModule.CUSTOMER,
            SearchModule.LEAD,
        ]

    async def test_returns_422_for_unknown_module(self, client, current_user_role):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/advanced",
                params={"q": "smith", "modules": "not_a_real_module"},
            )

        assert response.status_code == 422

    async def test_returns_422_for_invalid_date_param(self, client, current_user_role):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/advanced",
                params={"q": "smith", "date_from": "not-a-date"},
            )

        assert response.status_code == 422

    async def test_returns_422_for_invalid_extra_json(self, client, current_user_role):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/advanced",
                params={"q": "smith", "extra": "{not valid json"},
            )

        assert response.status_code == 422

    async def test_blocks_restricted_module_for_non_elevated_role(
        self, client, current_user_role
    ):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/advanced",
                params={
                    "q": "invoice",
                    "search_type": "filtered",
                    "modules": "audit_log",
                },
            )

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET /search/suggestions
# ---------------------------------------------------------------------------
class TestSuggestionsEndpoint:
    async def test_returns_ranked_suggestion_list(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.get_recent_searches.return_value = [
            _history_response(search_query="downtown condo")
        ]
        fake_service.get_statistics.return_value = _statistics_response(
            top_queries={"downtown lofts": 4}
        )

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/suggestions", params={"q": "down"}
            )

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)

    async def test_survives_statistics_validation_error(
        self, client, fake_service, current_user_role
    ):
        """A ValidationError from `get_statistics` must not break suggestions."""
        _set_role(current_user_role, "agent")
        fake_service.get_recent_searches.return_value = [
            _history_response(search_query="waterfront villa")
        ]
        fake_service.get_statistics.side_effect = ValidationError("no data yet")

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/suggestions", params={"q": "water"}
            )

        assert response.status_code == 200

    async def test_returns_422_when_query_blank(self, client, current_user_role):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.get(f"{API_PREFIX}/suggestions", params={"q": ""})

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /search/history
# ---------------------------------------------------------------------------
class TestListHistoryEndpoint:
    async def test_returns_200_with_paginated_items(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.list_history.return_value = SearchHistoryListResponse(
            items=[_history_response()],
            total=1,
            page=1,
            page_size=20,
            total_pages=1,
        )

        async with client as c:
            response = await c.get(f"{API_PREFIX}/history")

        assert response.status_code == 200
        assert response.json()["total"] == 1

    async def test_returns_422_for_invalid_sort_order(self, client, current_user_role):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/history", params={"sort_order": "sideways"}
            )

        assert response.status_code == 422

    async def test_forwards_module_and_search_type_filters(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.list_history.return_value = SearchHistoryListResponse(
            items=[], total=0, page=1, page_size=20, total_pages=0
        )

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/history",
                params={"module": "task", "search_type": "filtered"},
            )

        assert response.status_code == 200
        _, kwargs = fake_service.list_history.await_args
        assert kwargs["module"] == SearchModule.TASK
        assert kwargs["search_type"] == SearchType.FILTERED


# ---------------------------------------------------------------------------
# GET /search/history/recent
# ---------------------------------------------------------------------------
class TestRecentHistoryEndpoint:
    async def test_returns_200_with_list(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.get_recent_searches.return_value = [
            _history_response(search_query="a"),
            _history_response(search_query="b"),
        ]

        async with client as c:
            response = await c.get(f"{API_PREFIX}/history/recent")

        assert response.status_code == 200
        assert len(response.json()) == 2

    async def test_respects_limit_query_param(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.get_recent_searches.return_value = []

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/history/recent", params={"limit": 5}
            )

        assert response.status_code == 200
        _, kwargs = fake_service.get_recent_searches.await_args
        assert kwargs["limit"] == 5

    async def test_returns_422_when_limit_exceeds_maximum(
        self, client, current_user_role
    ):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/history/recent", params={"limit": 51}
            )

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET / DELETE /search/history/{history_id}
# ---------------------------------------------------------------------------
class TestSingleHistoryRecordEndpoints:
    async def test_get_history_returns_200(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        record_id = uuid.uuid4()
        fake_service.get_history.return_value = _history_response(id=record_id)

        async with client as c:
            response = await c.get(f"{API_PREFIX}/history/{record_id}")

        assert response.status_code == 200
        assert response.json()["id"] == str(record_id)

    async def test_get_history_returns_404_when_missing(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.get_history.side_effect = NotFoundError(
            "Search history record was not found."
        )

        async with client as c:
            response = await c.get(f"{API_PREFIX}/history/{uuid.uuid4()}")

        assert response.status_code == 404

    async def test_get_history_returns_422_for_malformed_uuid(
        self, client, current_user_role
    ):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.get(f"{API_PREFIX}/history/not-a-uuid")

        assert response.status_code == 422

    async def test_delete_history_returns_204(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.delete_history.return_value = None

        async with client as c:
            response = await c.delete(f"{API_PREFIX}/history/{uuid.uuid4()}")

        assert response.status_code == 204
        fake_service.delete_history.assert_awaited_once()

    async def test_delete_history_returns_404_when_missing(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.delete_history.side_effect = NotFoundError(
            "Search history record was not found."
        )

        async with client as c:
            response = await c.delete(f"{API_PREFIX}/history/{uuid.uuid4()}")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /search/history
# ---------------------------------------------------------------------------
class TestClearHistoryEndpoint:
    async def test_returns_200_with_deleted_count(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.clear_history.return_value = 4

        async with client as c:
            response = await c.delete(f"{API_PREFIX}/history")

        assert response.status_code == 200
        assert response.json()["deleted_count"] == 4


# ---------------------------------------------------------------------------
# GET /search/statistics
# ---------------------------------------------------------------------------
class TestStatisticsEndpoint:
    async def test_self_scope_returns_200_for_any_role(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "agent")
        fake_service.get_statistics.return_value = _statistics_response(
            total_searches=3
        )

        async with client as c:
            response = await c.get(f"{API_PREFIX}/statistics")

        assert response.status_code == 200
        assert response.json()["total_searches"] == 3
        _, kwargs = fake_service.get_statistics.await_args
        assert kwargs["user_id"] == current_user_role["id"]

    async def test_all_scope_forbidden_for_non_elevated_role(
        self, client, current_user_role
    ):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/statistics", params={"scope": "all"}
            )

        assert response.status_code == 403

    async def test_all_scope_returns_200_for_elevated_role(
        self, client, fake_service, current_user_role
    ):
        _set_role(current_user_role, "admin")
        fake_service.get_statistics.return_value = _statistics_response(
            total_searches=42
        )

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/statistics", params={"scope": "all"}
            )

        assert response.status_code == 200
        _, kwargs = fake_service.get_statistics.await_args
        assert kwargs["user_id"] is None

    async def test_returns_422_for_invalid_scope_value(
        self, client, current_user_role
    ):
        _set_role(current_user_role, "agent")

        async with client as c:
            response = await c.get(
                f"{API_PREFIX}/statistics", params={"scope": "everyone"}
            )

        assert response.status_code == 422