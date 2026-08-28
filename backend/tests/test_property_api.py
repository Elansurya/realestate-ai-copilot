"""
tests/test_property_api.py

Real-HTTP integration tests for the Property Management module
(app/api/v1/property.py -> PropertyService -> PropertyRepository ->
Property model), run against the project's actual PostgreSQL database
(no SQLite / no in-memory DB substitution) via `settings.DATABASE_URL`.

Fixture reuse:
    `db_session`, `app`, `client`, `admin_user`, `admin_client`,
    `sales_agent_user`, `sales_agent_client` are imported unchanged
    from `tests.test_booking_api`, which already wires an
    `AsyncSession` bound to the real database and an `httpx.AsyncClient`
    with `get_db` overridden to yield that session -- exactly the
    pattern `tests/conftest.py` uses for the payment test suite.

Scope: create, get, list, update, status transitions (incl. terminal
lock), agent assignment, soft delete / reactivate, search/filter/sort/
pagination, validation errors, duplicate/conflict cases, auth (401),
authorization/RBAC (403), and invalid/nonexistent IDs (404/422).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient

from tests.test_booking_api import (  # noqa: F401  (fixtures re-exported for pytest)
    admin_client,
    admin_user,
    app,
    client,
    db_session,
    sales_agent_client,
    sales_agent_user,
)

# Matches the convention already established in test_booking_api.py:
# the project relies on this module-level marker (not a global
# `asyncio_mode = auto` ini setting) to run async test functions.
pytestmark = pytest.mark.asyncio

PROPERTIES_PREFIX = "/api/v1/properties"


def _property_payload(**overrides) -> dict:
    payload = {
        "title": "Sunset Ridge 2BHK",
        "description": "A well-lit 2BHK close to the metro.",
        "property_type": "apartment",
        "listing_type": "sale",
        "furnishing": "semi_furnished",
        "price": 5500000,
        "area_sqft": 1200,
        "bedrooms": 2,
        "bathrooms": 2,
        "parking": 1,
        "address": "12 Sunset Ridge Road",
        "city": "Chennai",
        "state": "Tamil Nadu",
        "pincode": "600001",
        "latitude": 13.0827,
        "longitude": 80.2707,
        "owner_name": "Test Owner",
        "owner_phone": "9800000000",
        "owner_email": "owner@example.com",
        "is_featured": False,
    }
    payload.update(overrides)
    return payload


async def _create_property_via_api(admin_client: AsyncClient, **overrides) -> dict:
    resp = await admin_client.post(PROPERTIES_PREFIX, json=_property_payload(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# Authentication (401)
# --------------------------------------------------------------------------


async def test_list_properties_without_token_returns_401(client: AsyncClient) -> None:
    resp = await client.get(PROPERTIES_PREFIX)
    assert resp.status_code == 401


async def test_create_property_without_token_returns_401(client: AsyncClient) -> None:
    resp = await client.post(PROPERTIES_PREFIX, json=_property_payload())
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


async def test_create_property_success(admin_client: AsyncClient) -> None:
    body = await _create_property_via_api(admin_client)
    assert body["title"] == "Sunset Ridge 2BHK"
    assert body["property_status"] == "available"
    assert body["is_active"] is True
    assert body["property_code"].startswith("PROP-")
    uuid.UUID(body["uuid"])  # does not raise


async def test_create_property_agent_role_allowed(sales_agent_client: AsyncClient) -> None:
    body = await _create_property_via_api(sales_agent_client, pincode="600011")
    assert body["property_status"] == "available"


async def test_create_property_residential_requires_bedrooms(admin_client: AsyncClient) -> None:
    payload = _property_payload(pincode="600012")
    del payload["bedrooms"]
    resp = await admin_client.post(PROPERTIES_PREFIX, json=payload)
    assert resp.status_code == 422


async def test_create_property_rejects_status_incompatible_with_listing_type(
    admin_client: AsyncClient,
) -> None:
    payload = _property_payload(
        listing_type="rent", property_status="sold", pincode="600013"
    )
    resp = await admin_client.post(PROPERTIES_PREFIX, json=payload)
    assert resp.status_code == 400


async def test_create_property_duplicate_code_conflict(admin_client: AsyncClient) -> None:
    first = await _create_property_via_api(admin_client, pincode="600014")
    resp = await admin_client.post(
        PROPERTIES_PREFIX,
        json=_property_payload(
            property_code=first["property_code"], pincode="600015"
        ),
    )
    assert resp.status_code == 409


async def test_create_property_negative_price_rejected(admin_client: AsyncClient) -> None:
    resp = await admin_client.post(
        PROPERTIES_PREFIX, json=_property_payload(price=-100, pincode="600016")
    )
    assert resp.status_code == 422


async def test_create_property_zero_area_rejected(admin_client: AsyncClient) -> None:
    resp = await admin_client.post(
        PROPERTIES_PREFIX, json=_property_payload(area_sqft=0, pincode="600017")
    )
    assert resp.status_code == 422


async def test_create_property_invalid_pincode_rejected(admin_client: AsyncClient) -> None:
    resp = await admin_client.post(
        PROPERTIES_PREFIX, json=_property_payload(pincode="ABCDEF")
    )
    assert resp.status_code == 422


async def test_create_property_invalid_enum_rejected(admin_client: AsyncClient) -> None:
    payload = _property_payload(pincode="600018")
    payload["property_type"] = "castle"
    resp = await admin_client.post(PROPERTIES_PREFIX, json=payload)
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Read (single)
# --------------------------------------------------------------------------


async def test_get_property_by_uuid_success(admin_client: AsyncClient) -> None:
    created = await _create_property_via_api(admin_client, pincode="600019")
    resp = await admin_client.get(f"{PROPERTIES_PREFIX}/{created['uuid']}")
    assert resp.status_code == 200
    assert resp.json()["uuid"] == created["uuid"]


async def test_get_property_nonexistent_uuid_returns_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(f"{PROPERTIES_PREFIX}/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_get_property_malformed_uuid_returns_422(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(f"{PROPERTIES_PREFIX}/not-a-uuid")
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# List / search / filter / sort / pagination
# --------------------------------------------------------------------------


async def test_list_properties_default_excludes_soft_deleted(
    admin_client: AsyncClient,
) -> None:
    created = await _create_property_via_api(admin_client, pincode="600020")
    del_resp = await admin_client.delete(f"{PROPERTIES_PREFIX}/{created['uuid']}")
    assert del_resp.status_code == 200
    assert del_resp.json()["is_active"] is False

    list_resp = await admin_client.get(PROPERTIES_PREFIX, params={"page_size": 100})
    assert list_resp.status_code == 200
    codes = [item["property_code"] for item in list_resp.json()["items"]]
    assert created["property_code"] not in codes


async def test_list_properties_is_active_false_shows_only_deleted(
    admin_client: AsyncClient,
) -> None:
    created = await _create_property_via_api(admin_client, pincode="600021")
    await admin_client.delete(f"{PROPERTIES_PREFIX}/{created['uuid']}")

    resp = await admin_client.get(
        PROPERTIES_PREFIX, params={"is_active": "false", "page_size": 100}
    )
    assert resp.status_code == 200
    codes = [item["property_code"] for item in resp.json()["items"]]
    assert created["property_code"] in codes
    assert all(item["is_active"] is False for item in resp.json()["items"])


async def test_list_properties_filter_by_type(admin_client: AsyncClient) -> None:
    await _create_property_via_api(
        admin_client, property_type="studio", bedrooms=1, pincode="600022"
    )
    resp = await admin_client.get(
        PROPERTIES_PREFIX, params={"property_type": "studio", "page_size": 100}
    )
    assert resp.status_code == 200
    assert all(item["property_type"] == "studio" for item in resp.json()["items"])
    assert resp.json()["total"] >= 1


async def test_list_properties_search_by_title(admin_client: AsyncClient) -> None:
    unique_title = f"UniqueSearchTitle-{uuid.uuid4().hex[:8]}"
    await _create_property_via_api(admin_client, title=unique_title, pincode="600023")
    resp = await admin_client.get(
        PROPERTIES_PREFIX, params={"search": unique_title}
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["title"] == unique_title


async def test_list_properties_pagination(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(
        PROPERTIES_PREFIX, params={"page": 1, "page_size": 2}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) <= 2
    expected_pages = (body["total"] + 1) // 2 if body["total"] else 0
    assert body["total_pages"] == expected_pages


async def test_list_properties_sort_by_price_asc(admin_client: AsyncClient) -> None:
    await _create_property_via_api(admin_client, price=999999999, pincode="600024")
    resp = await admin_client.get(
        PROPERTIES_PREFIX,
        params={"sort_by": "price", "sort_order": "asc", "page_size": 100},
    )
    assert resp.status_code == 200
    prices = [Decimal(item["price"]) for item in resp.json()["items"]]
    assert prices == sorted(prices)


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------


async def test_update_property_partial_success(admin_client: AsyncClient) -> None:
    created = await _create_property_via_api(admin_client, pincode="600025")
    resp = await admin_client.put(
        f"{PROPERTIES_PREFIX}/{created['uuid']}", json={"price": 6000000, "bedrooms": 3}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert Decimal(body["price"]) == Decimal("6000000.00")
    assert body["bedrooms"] == 3


async def test_update_property_empty_body_returns_422(admin_client: AsyncClient) -> None:
    created = await _create_property_via_api(admin_client, pincode="600026")
    resp = await admin_client.put(f"{PROPERTIES_PREFIX}/{created['uuid']}", json={})
    assert resp.status_code == 422


async def test_update_property_nonexistent_returns_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.put(
        f"{PROPERTIES_PREFIX}/{uuid.uuid4()}", json={"price": 100}
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Status transitions
# --------------------------------------------------------------------------


async def test_status_transition_valid(admin_client: AsyncClient) -> None:
    created = await _create_property_via_api(admin_client, pincode="600027")
    resp = await admin_client.patch(
        f"{PROPERTIES_PREFIX}/{created['uuid']}/status",
        json={"property_status": "under_negotiation"},
    )
    assert resp.status_code == 200
    assert resp.json()["property_status"] == "under_negotiation"


async def test_status_transition_terminal_then_locked(admin_client: AsyncClient) -> None:
    created = await _create_property_via_api(admin_client, pincode="600028")
    sold = await admin_client.patch(
        f"{PROPERTIES_PREFIX}/{created['uuid']}/status",
        json={"property_status": "sold"},
    )
    assert sold.status_code == 200
    assert sold.json()["property_status"] == "sold"

    locked = await admin_client.patch(
        f"{PROPERTIES_PREFIX}/{created['uuid']}/status",
        json={"property_status": "available"},
    )
    assert locked.status_code == 409


async def test_status_transition_incompatible_with_listing_type(
    admin_client: AsyncClient,
) -> None:
    created = await _create_property_via_api(
        admin_client, listing_type="rent", pincode="600029"
    )
    resp = await admin_client.patch(
        f"{PROPERTIES_PREFIX}/{created['uuid']}/status",
        json={"property_status": "sold"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Agent assignment (Admin-only)
# --------------------------------------------------------------------------


async def test_assign_agent_success(
    admin_client: AsyncClient, admin_user, sales_agent_user
) -> None:
    created = await _create_property_via_api(admin_client, pincode="600030")
    resp = await admin_client.patch(
        f"{PROPERTIES_PREFIX}/{created['uuid']}/assign-agent",
        json={"assigned_agent_id": sales_agent_user.id},
    )
    assert resp.status_code == 200
    assert resp.json()["assigned_agent_id"] == sales_agent_user.id


async def test_assign_agent_nonexistent_agent_returns_404(admin_client: AsyncClient) -> None:
    created = await _create_property_via_api(admin_client, pincode="600031")
    resp = await admin_client.patch(
        f"{PROPERTIES_PREFIX}/{created['uuid']}/assign-agent",
        json={"assigned_agent_id": 9_999_999},
    )
    assert resp.status_code == 404


async def test_assign_agent_forbidden_for_sales_agent_role(
    sales_agent_client: AsyncClient, admin_client: AsyncClient, sales_agent_user
) -> None:
    created = await _create_property_via_api(admin_client, pincode="600032")
    resp = await sales_agent_client.patch(
        f"{PROPERTIES_PREFIX}/{created['uuid']}/assign-agent",
        json={"assigned_agent_id": sales_agent_user.id},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Soft delete / reactivate (Admin-only)
# --------------------------------------------------------------------------


async def test_soft_delete_and_reactivate_roundtrip(admin_client: AsyncClient) -> None:
    created = await _create_property_via_api(admin_client, pincode="600033")

    deleted = await admin_client.delete(f"{PROPERTIES_PREFIX}/{created['uuid']}")
    assert deleted.status_code == 200
    assert deleted.json()["is_active"] is False

    reactivated = await admin_client.patch(
        f"{PROPERTIES_PREFIX}/{created['uuid']}/reactivate"
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["is_active"] is True


async def test_soft_delete_forbidden_for_sales_agent_role(
    sales_agent_client: AsyncClient, admin_client: AsyncClient
) -> None:
    created = await _create_property_via_api(admin_client, pincode="600034")
    resp = await sales_agent_client.delete(f"{PROPERTIES_PREFIX}/{created['uuid']}")
    assert resp.status_code == 403


async def test_soft_delete_nonexistent_returns_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.delete(f"{PROPERTIES_PREFIX}/{uuid.uuid4()}")
    assert resp.status_code == 404