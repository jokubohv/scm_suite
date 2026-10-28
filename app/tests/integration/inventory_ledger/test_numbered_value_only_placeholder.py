"""Real numbered receipt, costing and DB trigger for the observed T-04 failure."""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from app.modules.inventory_ledger.facade import PostingPort
from app.modules.item_master.tables import item, item_inventory, item_type_definition
from app.modules.traceability_numbered.tables import lot
from app.tests.integration.inventory_ledger.conftest import _insert_location, _insert_status
from app.tests.integration.procurement.test_invoice_catalog_matching import _catalog_world

pytestmark = pytest.mark.integration


async def _physical_state(session, tenant_id):
    result = {}
    for table in ("inventory_ledger.inventory_balance", "inventory_ledger.inventory_balance_lot",
        "costing.item_cost", "costing.cost_layer", "costing.cost_ledger_entry",
        "traceability.serial_number", "traceability.serial_number_history"):
        result[table] = (await session.execute(text(f"SELECT to_jsonb(t) FROM {table} t WHERE tenant_id=:tenant ORDER BY id"),
            {"tenant":tenant_id})).scalars().all()
    return result


async def test_numbered_receipt_bill_placeholder_preserves_value_stock_and_physical_guards(db_session):
    world = await _catalog_world(db_session)
    tenant_id = world['tenant_id']
    location_id = await _insert_location(db_session, tenant_id, code='SYN-VALUE-WH')
    status_id = await _insert_status(db_session, tenant_id)
    type_id, item_id, lot_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await db_session.execute(insert(item_type_definition).values(id=type_id, tenant_id=tenant_id,
        code='LOT_NUMBERED', name='Synthetic numbered inventory', can_stock=True, requires_numbering=True))
    await db_session.execute(insert(item).values(id=item_id, tenant_id=tenant_id, item_type_id=type_id,
        item_number='SYN-VALUE-LOT', display_name='Synthetic value-only lot item', base_uom_id=None))
    await db_session.execute(insert(item_inventory).values(tenant_id=tenant_id, item_id=item_id, costing_method='FIFO'))
    await db_session.execute(insert(lot).values(id=lot_id, tenant_id=tenant_id, item_id=item_id,
        lot_number='SYN-VALUE-LOT-001', expiry_date=None))
    port = PostingPort(db_session, tenant_id)
    assert port.costing is not None
    common = {'item_id':str(item_id), 'location_id':str(location_id), 'inventory_status_id':str(status_id)}
    receipt = await port.post_receipt(transaction_date='2026-10-28', source_doc_type='ITEM_RECEIPT',
        source_doc_id=str(uuid.uuid4()), idempotency_key='synthetic-numbered-value-receipt', movements=[{
            **common, 'qty':'5', 'unit_cost':'10', 'value_base':'50', 'lot_id':str(lot_id),
            'details':[{'qty':'5', 'value_base':'50', 'lot_id':str(lot_id), 'inventory_status_id':str(status_id), 'ownership':'OWNED'}]}])
    assert Decimal(receipt['total_qty_base']) == 5 and Decimal(receipt['total_value_base']) == 50
    before = await _physical_state(db_session, tenant_id)
    assert len(before['costing.cost_layer']) == 1
    assert sum(Decimal(str(row['qty_on_hand'])) for row in before['inventory_ledger.inventory_balance']) == 5
    # The real physical guard remains in force; the failed transaction, including
    # costing work before its detail insert, is rolled back to a savepoint.
    with pytest.raises(IntegrityError, match='TN003'):
        async with db_session.begin_nested():
            await port.post_receipt(transaction_date='2026-10-28', source_doc_type='ITEM_RECEIPT',
                source_doc_id=str(uuid.uuid4()), idempotency_key='synthetic-physical-without-lot', movements=[{
                    **common, 'qty':'1', 'unit_cost':'10', 'value_base':'10',
                    'details':[{'qty':'1', 'inventory_status_id':str(status_id), 'ownership':'OWNED'}]}])
    assert await _physical_state(db_session, tenant_id) == before
    print('REAL_NUMBERED_PREFLIGHT receipt_qty=5 receipt_value=50 fifo_layers=1 physical_missing_lot=TN003 rollback=unchanged')
    source_id = str(uuid.uuid4())
    command = dict(transaction_date='2026-10-28', source_doc_type='VENDOR_BILL', source_doc_id=source_id,
        idempotency_key='synthetic-numbered-value-bill', movements=[{
            **common, 'qty':'0', 'value_base':'25', 'movement_type':'VENDOR_BILL', 'source_line_ref':'1',
            'details':[{'qty':'0', 'inventory_status_id':str(status_id), 'ownership':'OWNED'}]}])
    posted = await port.post_vendor_bill(**command)
    assert Decimal(posted['total_qty_base']) == 0 and Decimal(posted['total_value_base']) == 25
    transaction_id = uuid.UUID(posted['transaction_id'])
    header = (await db_session.execute(text('SELECT transaction_type,total_qty_base,total_value_base FROM inventory_ledger.inventory_transaction WHERE tenant_id=:tenant AND id=:id'),
        {'tenant':tenant_id, 'id':transaction_id})).one()
    assert tuple(header) == ('T-04', Decimal('0'), Decimal('25'))
    lines = (await db_session.execute(text('SELECT qty_base,value_base,source_line_ref FROM inventory_ledger.inventory_transaction_line WHERE tenant_id=:tenant AND inventory_transaction_id=:id'),
        {'tenant':tenant_id, 'id':transaction_id})).all()
    assert [tuple(row) for row in lines] == [(Decimal('0'), Decimal('25'), '1')]
    details = (await db_session.execute(text('SELECT count(*) FROM inventory_ledger.inventory_transaction_detail WHERE tenant_id=:tenant AND inventory_transaction_id=:id'),
        {'tenant':tenant_id, 'id':transaction_id})).scalar_one()
    assert details == 0  # an empty sentinel is not a physical allocation or value fact
    replay = await port.post_vendor_bill(**command)
    assert replay['transaction_id'] == posted['transaction_id'] and replay['replayed']
    assert (await db_session.execute(text("SELECT count(*) FROM inventory_ledger.inventory_transaction WHERE tenant_id=:tenant AND source_doc_id=:source"),
        {'tenant':tenant_id, 'source':uuid.UUID(source_id)})).scalar_one() == 1
    assert await _physical_state(db_session, tenant_id) == before
