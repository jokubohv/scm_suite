"""Invoice matching must not mistake a truncated or foreign directory for absence.

Real procurement router/auth/PostgreSQL; the existing outer transaction rolls back
all explicitly synthetic tenants and catalog rows. No stock or receipt is posted.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert

from app.api.deps import token_service
from app.core.models.platform import app_user, permission_scope, role, role_permission, tenant, user_identity, user_role
from app.modules.item_master.tables import item, item_type_definition
from app.modules.procurement.tables import item_vendor, vendor

pytestmark = pytest.mark.integration


async def _catalog_world(session):
    """Only the typed rows this directory needs; no unrelated item-source service joins."""
    tenant_id, identity_id, user_id, type_id, item_id = (uuid.uuid4() for _ in range(5))
    suffix = tenant_id.hex[:12]
    await session.execute(insert(tenant).values(
        id=tenant_id, tenant_id=tenant_id, code=f"INV-MATCH-{suffix}",
        name="Synthetic invoice catalog test tenant", status="ACTIVE", base_currency_code="USD",
    ))
    await session.execute(insert(user_identity).values(
        id=identity_id, email=f"invoice-{suffix}@example.invalid", display_name="Synthetic catalog reader", status="ACTIVE",
    ))
    await session.execute(insert(app_user).values(
        id=user_id, tenant_id=tenant_id, identity_id=identity_id, email=f"invoice-{suffix}@example.invalid",
        status="ACTIVE", membership_status="ACTIVE", token_version=0,
    ))
    role_id, scope_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(insert(permission_scope).values(
        id=scope_id, tenant_id=tenant_id, scope="procurement.vendor.read", domain="procurement",
        action="read", is_assignable=True,
    ))
    await session.execute(insert(role).values(
        id=role_id, tenant_id=tenant_id, code="invoice_catalog_reader", name="Synthetic invoice catalog reader",
    ))
    await session.execute(insert(role_permission).values(tenant_id=tenant_id, role_id=role_id, scope_id=scope_id))
    await session.execute(insert(user_role).values(tenant_id=tenant_id, user_id=user_id, role_id=role_id, effective_to=None))
    await session.execute(insert(item_type_definition).values(
        id=type_id, tenant_id=tenant_id, code="INVENTORY", name="Synthetic inventory", can_stock=True,
    ))
    await session.execute(insert(item).values(
        id=item_id, tenant_id=tenant_id, item_type_id=type_id,
        item_number=f"INV-MATCH-ITEM-{suffix}", display_name="Synthetic catalog item", base_uom_id=None,
    ))
    token, _ = token_service().issue_access_token(
        identity_id=str(identity_id), app_user_id=str(user_id), tenant_id=str(tenant_id),
        token_version=0, scopes=frozenset({"procurement.vendor.read"}),
    )
    return {"tenant_id": tenant_id, "item_id": item_id, "type_id": type_id, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture()
async def world(db_session):
    return await _catalog_world(db_session)


async def test_invoice_matching_catalog_queries_are_complete_exact_and_tenant_scoped(client, world, db_session):
    foreign = await _catalog_world(db_session)
    prefix = f"INV-MATCH-{uuid.uuid4().hex[:8]}"
    # Deliberately reverse the UUID relation at the first page boundary. An `id > cursor`
    # predicate combined with vendor_number ordering repeats A and omits C/D/E.
    ids = sorted(uuid.uuid4() for _ in range(5))
    vendor_ids = [ids[4], ids[3], ids[0], ids[2], ids[1]]
    numbers = [f"{prefix}-{letter}" for letter in "ABCDE"]
    exact_tax = f"TAX-{prefix}"
    for index, ident in enumerate(vendor_ids):
        await db_session.execute(insert(vendor).values(
            id=ident, tenant_id=world["tenant_id"], vendor_number=numbers[index],
            name=f"Synthetic invoice supplier {prefix} {index}", currency_code="USD",
            tax_id=exact_tax if index in (1, 3) else f"{exact_tax}-OTHER-{index}",
            payment_terms=None, credit_limit=None, inactive_at=None,
        ))
    foreign_vendor_id = uuid.uuid4()
    await db_session.execute(insert(vendor).values(
        id=foreign_vendor_id, tenant_id=foreign["tenant_id"], vendor_number=numbers[1],
        name=f"Synthetic foreign supplier {prefix}", currency_code="USD", tax_id=exact_tax,
    ))

    # A supplier SKU can be ambiguous across several products, so exact filtering
    # must still page. A near-match and a foreign-tenant row must never become candidates.
    items = []
    for index in range(4):
        item_id = uuid.uuid4()
        await db_session.execute(insert(item).values(
            id=item_id, tenant_id=world["tenant_id"], item_type_id=world["type_id"],
            item_number=f"{prefix}-ITEM-{index}", display_name=f"Synthetic matching item {index}", base_uom_id=None,
        ))
        items.append(item_id)
    items.sort()
    link_ids = sorted((uuid.uuid4() for _ in range(4)), reverse=True)
    supplier_sku = f"SKU-{prefix}"
    for index, item_id in enumerate(items):
        await db_session.execute(insert(item_vendor).values(
            id=link_ids[index], tenant_id=world["tenant_id"], item_id=item_id,
            vendor_id=vendor_ids[1], vendor_item_code=supplier_sku if index < 3 else f"{supplier_sku}-EXTRA",
            purchase_uom_id=None, currency_code="USD",
        ))
    foreign_link_id = uuid.uuid4()
    await db_session.execute(insert(item_vendor).values(
        id=foreign_link_id, tenant_id=foreign["tenant_id"], item_id=foreign["item_id"],
        vendor_id=foreign_vendor_id, vendor_item_code=supplier_sku, currency_code="USD",
    ))

    # Public API order and continuation, independent of SQL implementation details.
    collected = []
    cursor = None
    for _ in range(4):
        params = {"q": prefix, "limit": 2}
        if cursor:
            params["cursor"] = cursor
        response = await client.get("/api/v1/vendors", params=params, headers=world["headers"])
        assert response.status_code == 200, response.text
        page = response.json()
        collected.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None, "vendor pagination must terminate"
    assert collected == [str(value) for value in vendor_ids], "vendor_number order must not lose/repeat noncorrelated UUIDs"
    assert page["matching_contract_version"] == 1

    tax_matches = []
    cursor = None
    for _ in range(3):
        params = {"tax_id": exact_tax, "limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = await client.get("/api/v1/vendors", params=params, headers=world["headers"])
        assert response.status_code == 200, response.text
        page = response.json()
        tax_matches.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert tax_matches == [str(vendor_ids[1]), str(vendor_ids[3])]

    exact = await client.get("/api/v1/vendors", params={"vendor_number": numbers[1]}, headers=world["headers"])
    assert exact.status_code == 200, exact.text
    assert [row["id"] for row in exact.json()["items"]] == [str(vendor_ids[1])]
    scoped = await client.get("/api/v1/vendors", params={"vendor_number": numbers[1], "tax_id": exact_tax}, headers=foreign["headers"])
    assert scoped.status_code == 200, scoped.text
    assert [row["id"] for row in scoped.json()["items"]] == [str(foreign_vendor_id)]
    absent = await client.get("/api/v1/vendors", params={"vendor_number": numbers[0]}, headers=foreign["headers"])
    assert absent.status_code == 200 and absent.json()["items"] == []

    links = []
    cursor = None
    for _ in range(4):
        params = {"vendor_id": str(vendor_ids[1]), "vendor_item_code": supplier_sku, "limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = await client.get("/api/v1/item-vendors", params=params, headers=world["headers"])
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) <= 1, "requested page size must be honored"
        links.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert links == [str(value) for value in link_ids[:3]], "complete exact supplier SKU matches in existing item/vendor order"
    assert page["matching_contract_version"] == 1
    foreign_links = await client.get("/api/v1/item-vendors", params={"vendor_item_code": supplier_sku}, headers=foreign["headers"])
    assert foreign_links.status_code == 200, foreign_links.text
    assert [row["id"] for row in foreign_links.json()["items"]] == [str(foreign_link_id)]
    denied_vendor = await client.get("/api/v1/item-vendors", params={"vendor_id": str(vendor_ids[1]), "vendor_item_code": supplier_sku}, headers=foreign["headers"])
    assert denied_vendor.status_code == 200 and denied_vendor.json()["items"] == []
