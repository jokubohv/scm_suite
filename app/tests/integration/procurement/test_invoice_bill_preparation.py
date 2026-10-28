"""P4 bill import contract: real router/auth/PG, synthetic fixtures rolled back.

These diagnostic fixtures deliberately seed document state without posting stock;
the independently owned joined journey establishes real PO/receipt lifecycle.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import func, insert, select, update

from app.api.deps import token_service
from app.core.models.platform import app_user, feature_flag, permission_scope, role, role_permission
from app.modules.item_master.tables import item, uom, uom_type
from app.modules.procurement.tables import (
    item_receipt, item_receipt_line, purchase_order, purchase_order_line,
    vendor, vendor_bill,
)
from app.tests.integration.procurement.test_invoice_catalog_matching import _catalog_world

pytestmark = pytest.mark.integration


@pytest.fixture()
async def world(db_session):
    world = await _catalog_world(db_session)
    tenant_id = world['tenant_id']
    actor = (await db_session.execute(select(app_user).where(app_user.c.tenant_id == tenant_id))).mappings().one()
    role_id = (await db_session.execute(select(role.c.id).where(role.c.tenant_id == tenant_id))).scalar_one()
    scopes = {'procurement.vendor.read', 'procurement.po.read', 'procurement.receipt.read',
              'procurement.bill.read', 'procurement.bill.write', 'procurement.bill.match'}
    for scope in scopes - {'procurement.vendor.read'}:
        scope_id = uuid.uuid4()
        await db_session.execute(insert(permission_scope).values(id=scope_id, tenant_id=tenant_id,
            scope=scope, domain='procurement', action=scope.rsplit('.', 1)[1], is_assignable=True))
        await db_session.execute(insert(role_permission).values(tenant_id=tenant_id, role_id=role_id, scope_id=scope_id))
    token, _ = token_service().issue_access_token(identity_id=str(actor['identity_id']),
        app_user_id=str(actor['id']), tenant_id=str(tenant_id), token_version=0, scopes=frozenset(scopes))
    world['headers'] = {'Authorization': f'Bearer {token}'}
    await db_session.execute(insert(feature_flag).values(id=uuid.uuid4(), tenant_id=tenant_id,
        flag_key='procurement.p2p.enabled', is_enabled=True, source='TEST'))
    type_id, unit_id = uuid.uuid4(), uuid.uuid4()
    await db_session.execute(insert(uom_type).values(id=type_id, tenant_id=tenant_id, code='SYN-P4', name='Synthetic units', base_uom_id=None))
    await db_session.execute(insert(uom).values(id=unit_id, tenant_id=tenant_id, uom_type_id=type_id,
        code='SYN-UNIT', name='Synthetic unit', is_base=True))
    world['item_id'] = uuid.uuid4()
    await db_session.execute(insert(item).values(id=world['item_id'], tenant_id=tenant_id,
        item_type_id=world['type_id'], item_number='SYN-P4-BILL-ITEM',
        display_name='Synthetic bill item', base_uom_id=unit_id))
    world['uom_id'] = unit_id
    world['vendor_id'] = uuid.uuid4()
    await db_session.execute(insert(vendor).values(id=world['vendor_id'], tenant_id=tenant_id,
        vendor_number='SYN-P4-VENDOR', name='Synthetic bill supplier', currency_code='USD'))
    world['documents'] = []
    stamp = datetime(2026, 10, 28, tzinfo=timezone.utc)
    for index in range(3):
        po_id, po_line_id, receipt_id, receipt_line_id = (uuid.uuid4() for _ in range(4))
        await db_session.execute(insert(purchase_order).values(id=po_id, tenant_id=tenant_id,
            document_number=f'SYN-P4-PO-{index}', transaction_date=date(2026, 10, 28),
            vendor_id=world['vendor_id'], status='RECEIVED', currency_code='USD', created_at=stamp,
            subtotal=Decimal('1500'), total=Decimal('1500')))
        await db_session.execute(insert(purchase_order_line).values(id=po_line_id, tenant_id=tenant_id,
            purchase_order_id=po_id, line_number=1, item_id=world['item_id'], qty_ordered=Decimal('600'),
            qty_received=Decimal('600'), uom_id=unit_id, unit_price=Decimal('2.5'), currency_code='USD'))
        await db_session.execute(insert(item_receipt).values(id=receipt_id, tenant_id=tenant_id,
            document_number=f'SYN-P4-IR-{index}', vendor_id=world['vendor_id'], purchase_order_id=po_id,
            status='POSTED', received_at=stamp, currency_code='USD'))
        await db_session.execute(insert(item_receipt_line).values(id=receipt_line_id, tenant_id=tenant_id,
            item_receipt_id=receipt_id, purchase_order_line_id=po_line_id, line_number=1,
            item_id=world['item_id'], qty_received=Decimal('600'), uom_id=unit_id,
            unit_cost=Decimal('2.5'), currency_code='USD'))
        world['documents'].append({'po_id': po_id, 'po_line_id': po_line_id,
            'receipt_id': receipt_id, 'receipt_line_id': receipt_line_id})
    return world


def payload(world, index=0):
    doc = world['documents'][index]
    return {'vendor_id': str(world['vendor_id']), 'vendor_bill_number': f'SYN-P4-BILL-{index}',
        'transaction_date': '2026-10-28', 'currency_code': 'USD', 'exchange_rate': '1',
        'purchase_order_id': str(doc['po_id']), 'item_receipt_id': str(doc['receipt_id']),
        'lines': [{'item_id': str(world['item_id']), 'uom_id': str(world['uom_id']),
            'qty': '600', 'unit_price': '2.5', 'amount': '1500',
            'purchase_order_line_id': str(doc['po_line_id']), 'item_receipt_line_id': str(doc['receipt_line_id'])}]}


async def test_bill_import_readback_and_same_key_replay_preserve_units_amount_links(client, world, db_session):
    # Public reads first prove the typed fixture, actual grants and API are healthy.
    assert (await client.get('/api/v1/purchase-orders', headers=world['headers'])).status_code == 200
    body = payload(world)
    headers = {**world['headers'], 'Idempotency-Key': 'synthetic-p4-readback'}
    created = await client.post('/api/v1/vendor-bills', json=body, headers=headers)
    assert created.status_code == 201, created.text
    detail = await client.get('/api/v1/vendor-bills/' + created.json()['id'], headers=world['headers'])
    assert detail.status_code == 200, detail.text
    bill = detail.json()
    assert bill['status'] == 'DRAFT'
    line = bill['lines'][0]
    assert line.get('uom_id') == str(world['uom_id']), 'readback must retain the reviewed base UOM'
    assert Decimal(line['amount']) == Decimal('1500')
    assert Decimal(line['qty']) == Decimal('600') and Decimal(line['unit_price']) == Decimal('2.5')
    assert line['purchase_order_line_id'] == body['lines'][0]['purchase_order_line_id']
    assert line['item_receipt_line_id'] == body['lines'][0]['item_receipt_line_id']
    assert bill['match']['match_status'] == 'PENDING'
    replay = await client.post('/api/v1/vendor-bills', json=body, headers=headers)
    assert replay.status_code == 201 and replay.json()['id'] == bill['id']
    assert (await db_session.execute(select(func.count()).select_from(vendor_bill).where(vendor_bill.c.tenant_id == world['tenant_id']))).scalar_one() == 1
    # A freight carrier's landed bill legitimately references another vendor's
    # goods receipt. Preserve that established branch while validating goods.
    carrier_id = uuid.uuid4()
    await db_session.execute(insert(vendor).values(id=carrier_id, tenant_id=world['tenant_id'],
        vendor_number='SYN-P4-CARRIER', name='Synthetic carrier', currency_code='USD'))
    landed = payload(world, 1)
    landed['vendor_id'] = str(carrier_id)
    landed['lines'][0]['is_landed_cost'] = True
    carrier = await client.post('/api/v1/vendor-bills', json=landed,
        headers={**world['headers'], 'Idempotency-Key': 'synthetic-p4-carrier'})
    assert carrier.status_code == 201, carrier.text
    assert carrier.json()['lines'][0]['is_landed_cost'] is True
    # Base-only billing is Serebo's new import policy. Existing manual SCM bills
    # may use the same explicit nonbase UOM as their PO and receipt.
    alternate = uuid.uuid4()
    unit_type = (await db_session.execute(select(uom.c.uom_type_id).where(uom.c.id == world['uom_id']))).scalar_one()
    await db_session.execute(insert(uom).values(id=alternate, tenant_id=world['tenant_id'],
        uom_type_id=unit_type, code='SYN-PAIR', name='Synthetic pair', conversion_factor=Decimal('2')))
    manual_doc = world['documents'][2]
    await db_session.execute(update(purchase_order_line).where(purchase_order_line.c.id == manual_doc['po_line_id']).values(uom_id=alternate))
    await db_session.execute(update(item_receipt_line).where(item_receipt_line.c.id == manual_doc['receipt_line_id']).values(uom_id=alternate))
    manual = payload(world, 2)
    manual['lines'][0]['uom_id'] = str(alternate)
    permitted = await client.post('/api/v1/vendor-bills', json=manual,
        headers={**world['headers'], 'Idempotency-Key': 'synthetic-p4-manual-uom'})
    assert permitted.status_code == 201, permitted.text
    assert permitted.json()['lines'][0]['uom_id'] == str(alternate)


async def test_bill_import_rejects_mismatched_header_line_ancestry_without_bill(client, world, db_session):
    body = payload(world)
    # All UUIDs exist in the same tenant: FK checks alone cannot reject this mistake.
    body['lines'][0]['purchase_order_line_id'] = str(world['documents'][1]['po_line_id'])
    response = await client.post('/api/v1/vendor-bills', json=body,
        headers={**world['headers'], 'Idempotency-Key': 'synthetic-p4-wrong-ancestry'})
    assert response.status_code == 422, response.text
    assert (await db_session.execute(select(func.count()).select_from(vendor_bill).where(vendor_bill.c.tenant_id == world['tenant_id']))).scalar_one() == 0
    corrected = await client.post('/api/v1/vendor-bills', json=payload(world),
        headers={**world['headers'], 'Idempotency-Key': 'synthetic-p4-corrected-ancestry'})
    assert corrected.status_code == 201, corrected.text


async def test_bill_source_pages_and_exact_reference_lookup_do_not_claim_false_absence(client, world):
    bill_ids = []
    for index in range(3):
        response = await client.post('/api/v1/vendor-bills', json=payload(world, index),
            headers={**world['headers'], 'Idempotency-Key': f'synthetic-p4-page-{index}'})
        assert response.status_code == 201, response.text
        bill_ids.append(response.json()['id'])
    expected = {'purchase-orders': {str(doc['po_id']) for doc in world['documents']},
        'item-receipts': {str(doc['receipt_id']) for doc in world['documents']}, 'vendor-bills': set(bill_ids)}
    for resource, ids in expected.items():
        found, cursor = [], None
        for _ in range(4):
            response = await client.get('/api/v1/' + resource,
                params={'limit': 1, **({'cursor': cursor} if cursor else {})}, headers=world['headers'])
            assert response.status_code == 200, response.text
            page = response.json()
            assert len(page['items']) <= 1
            found.extend(row['id'] for row in page['items'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        assert cursor is None and len(found) == len(ids) and set(found) == ids, f'{resource} must not truncate/repeat'
        if resource != 'vendor-bills':
            # An exactly-full final page is valid, but an old truncating service
            # must not gain that guarantee merely through a response default.
            terminal = await client.get('/api/v1/' + resource,
                params={'limit': len(ids)}, headers=world['headers'])
            assert terminal.status_code == 200, terminal.text
            page = terminal.json()
            assert len(page['items']) == len(ids) and page['next_cursor'] is None
            assert page.get('pagination_contract_version') == 1
            from app.api.schemas.procurement import PurchaseOrderPage, ReceiptPage
            page_model = PurchaseOrderPage if resource == 'purchase-orders' else ReceiptPage
            legacy = {key: value for key, value in page.items() if key != 'pagination_contract_version'}
            assert page_model.model_validate(legacy).model_dump().get('pagination_contract_version') is None
    exact = await client.get('/api/v1/vendor-bills', params={'vendor_id': str(world['vendor_id']),
        'vendor_bill_number': 'SYN-P4-BILL-1'}, headers=world['headers'])
    assert exact.status_code == 200 and [row['id'] for row in exact.json()['items']] == [bill_ids[1]]
    assert exact.json()['bill_preparation_contract_version'] == 1
    absent = await client.get('/api/v1/vendor-bills', params={'vendor_id': str(world['vendor_id']),
        'vendor_bill_number': 'SYN-P4-NONEXISTENT'}, headers=world['headers'])
    assert absent.status_code == 200 and absent.json()['items'] == []
