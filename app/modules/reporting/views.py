"""FS-15 reporting read models: 21 read-only views in schema ``reporting`` (V-01..V-21).

The SQL is deliberately kept here (not in a model module) because FS-15 owns **no base tables**; the
migration imports these definitions. Plain views use ``security_invoker=true`` so the caller's RLS
applies. Five heavy projections are materialised (V-04, V-11, V-12, V-16, V-21) and are refreshed by the
``reporting.refresh_views`` job. Views over later-wave contexts are created conditionally by the
migration: if the source relation does not exist yet, a typed empty fallback keeps the contract's 21
view names available (the real definition is used as soon as the owning migration has landed).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ViewSpec:
    number: str
    name: str
    purpose: str
    materialized: bool
    order_key: str
    cost_visible: bool
    sql: str
    fallback_sql: str = ""


def _v(number: str, name: str, purpose: str, order_key: str, sql: str, *,
       materialized: bool = False, cost_visible: bool = False, fallback_sql: str = "") -> ViewSpec:
    return ViewSpec(number, name, purpose, materialized, order_key, cost_visible, sql.strip(), fallback_sql.strip())


V01 = _v("V-01", "v_stock_ledger", "Posting-line stock ledger", "transaction_id, line_id", """
SELECT l.tenant_id, t.id AS transaction_id, l.id AS line_id,
       t.transaction_type, t.transaction_date, t.posting_at, t.ledger_sequence,
       t.source_doc_type, t.source_doc_id, t.external_ref, t.cost_estimated,
       l.item_id, i.item_number, i.display_name AS item_name,
       l.location_id, loc.code AS location_code,
       l.inventory_status_id, st.code AS status_code,
       l.ownership, l.lot_id, lot.lot_number, l.serial_id, sn.serial_number,
       l.qty_uom, l.uom_id, l.qty_base, l.value_base, l.value_txn, l.unit_cost,
       l.qty_class, l.movement_type, l.movement_direction,
       l.class_ref, l.department_ref, l.subsidiary_ref, l.cost_object_id,
       l.transaction_date AS effective_date
  FROM inventory_ledger.inventory_transaction_line l
  JOIN inventory_ledger.inventory_transaction t
    ON t.tenant_id = l.tenant_id AND t.id = l.inventory_transaction_id
  LEFT JOIN item_master.item i ON i.tenant_id = l.tenant_id AND i.id = l.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = l.tenant_id AND loc.id = l.location_id
  LEFT JOIN traceability.inventory_status st ON st.tenant_id = l.tenant_id AND st.id = l.inventory_status_id
  LEFT JOIN traceability.lot lot ON lot.tenant_id = l.tenant_id AND lot.id = l.lot_id
  LEFT JOIN traceability.serial_number sn ON sn.tenant_id = l.tenant_id AND sn.id = l.serial_id
""")

V02 = _v("V-02", "v_inventory_balance", "Balance with item/location/status names", "item_id, location_id, inventory_status_id", """
SELECT b.tenant_id, b.item_id, i.item_number, i.display_name AS item_name,
       b.location_id, loc.code AS location_code, b.inventory_status_id, st.code AS status_code,
       b.ownership, b.lot_id, b.serial_id,
       b.qty_on_hand, b.qty_committed, b.qty_available,
       b.qty_on_order, b.qty_backordered,
       COALESCE(t.qty_in_transit, 0) AS in_transit,
       b.value_base, b.last_ledger_sequence, b.last_posting_at
  FROM inventory_ledger.inventory_balance b
  LEFT JOIN item_master.item i ON i.tenant_id = b.tenant_id AND i.id = b.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = b.tenant_id AND loc.id = b.location_id
  LEFT JOIN traceability.inventory_status st ON st.tenant_id = b.tenant_id AND st.id = b.inventory_status_id
  LEFT JOIN (
        SELECT tenant_id, item_id, to_location_id, SUM(qty_in_transit) AS qty_in_transit
          FROM inventory_ledger.inventory_in_transit
         WHERE status IN ('IN_TRANSIT', 'PARTIALLY_RECEIVED')
         GROUP BY tenant_id, item_id, to_location_id
  ) t ON t.tenant_id = b.tenant_id AND t.item_id = b.item_id AND t.to_location_id = b.location_id
""")

V03 = _v("V-03", "v_inventory_valuation", "Value per cost object/position", "item_id, location_id", """
SELECT ic.tenant_id, ic.item_id, i.item_number, i.display_name AS item_name,
       ic.location_id, loc.code AS location_code, ic.location_costing_group_id, g.code AS cost_group_code,
       ic.lot_id, ic.serial_id, ic.costing_method, ic.qty_on_hand,
       ic.avg_cost, COALESCE(sc.unit_cost, ic.avg_cost) AS standard_cost,
       ic.value_base, ic.last_purchase_price, ic.status, ic.cost_provisional
  FROM costing.item_cost ic
  LEFT JOIN item_master.item i ON i.tenant_id = ic.tenant_id AND i.id = ic.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = ic.tenant_id AND loc.id = ic.location_id
  LEFT JOIN costing.location_costing_group g ON g.tenant_id = ic.tenant_id AND g.id = ic.location_costing_group_id
  LEFT JOIN LATERAL (
      SELECT sc.unit_cost FROM costing.standard_cost sc
       WHERE sc.tenant_id = ic.tenant_id AND sc.item_id = ic.item_id
         AND sc.effective_from <= CURRENT_DATE
         AND (sc.effective_to IS NULL OR sc.effective_to > CURRENT_DATE)
       ORDER BY sc.effective_from DESC, sc.id DESC LIMIT 1
  ) sc ON TRUE
""")

V04 = _v("V-04", "v_inventory_valuation_detail", "Valuation by layer", "layer_date, layer_id", """
SELECT cl.tenant_id, cl.id AS layer_id, cl.layer_date, cl.layer_seq,
       ic.item_id, i.item_number, i.display_name AS item_name,
       ic.location_id, loc.code AS location_code,
       cl.qty_original, cl.qty_remaining, cl.unit_cost, cl.value_remaining,
       cl.status, ic.costing_method, cl.estimated
  FROM costing.cost_layer cl
  JOIN costing.item_cost ic ON ic.tenant_id = cl.tenant_id AND ic.id = cl.item_cost_id
  LEFT JOIN item_master.item i ON i.tenant_id = ic.tenant_id AND i.id = ic.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = ic.tenant_id AND loc.id = ic.location_id
""", materialized=True, cost_visible=True)

V05 = _v("V-05", "v_inventory_snapshot", "Current snapshot per item/location/status", "item_id, location_id, inventory_status_id", """
SELECT b.tenant_id, b.item_id, i.item_number, i.display_name AS item_name,
       b.location_id, loc.code AS location_code, b.inventory_status_id, st.code AS status_code,
       b.qty_on_hand, b.value_base, ic.avg_cost,
       lm.last_movement_date
  FROM inventory_ledger.inventory_balance b
  LEFT JOIN item_master.item i ON i.tenant_id = b.tenant_id AND i.id = b.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = b.tenant_id AND loc.id = b.location_id
  LEFT JOIN traceability.inventory_status st ON st.tenant_id = b.tenant_id AND st.id = b.inventory_status_id
  LEFT JOIN costing.item_cost ic ON ic.tenant_id = b.tenant_id AND ic.item_id = b.item_id
                                AND ic.location_id = b.location_id
  LEFT JOIN LATERAL (
      SELECT MAX(l.transaction_date) AS last_movement_date
        FROM inventory_ledger.inventory_transaction_line l
       WHERE l.tenant_id = b.tenant_id AND l.item_id = b.item_id AND l.location_id = b.location_id
  ) lm ON TRUE
""")

V06 = _v("V-06", "v_negative_inventory", "Underwater positions", "item_id, location_id", """
SELECT b.tenant_id, b.item_id, i.item_number, i.display_name AS item_name,
       b.location_id, loc.code AS location_code, b.inventory_status_id,
       b.qty_on_hand, ic.avg_cost AS last_cost, b.value_base,
       (b.value_base < 0) AS estimated_negative
  FROM inventory_ledger.inventory_balance b
  LEFT JOIN item_master.item i ON i.tenant_id = b.tenant_id AND i.id = b.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = b.tenant_id AND loc.id = b.location_id
  LEFT JOIN costing.item_cost ic ON ic.tenant_id = b.tenant_id AND ic.item_id = b.item_id
                                AND ic.location_id = b.location_id
 WHERE b.qty_on_hand < 0
""", cost_visible=True)

V07 = _v("V-07", "v_open_commitments", "Active commitments with backlog", "commitment_id", """
SELECT c.tenant_id, c.id AS commitment_id, c.source_type, c.source_line_ref, c.external_ref,
       c.item_id, i.item_number, i.display_name AS item_name,
       c.location_id, loc.code AS location_code, c.inventory_status_id, c.ownership,
       c.qty_committed, c.qty_fulfilled, c.qty_backordered, c.status, c.priority,
       c.required_by_date, c.committed_at,
       (c.qty_committed - c.qty_fulfilled) AS qty_open
  FROM inventory_ledger.inventory_commitment c
  LEFT JOIN item_master.item i ON i.tenant_id = c.tenant_id AND i.id = c.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = c.tenant_id AND loc.id = c.location_id
 WHERE c.status IN ('ACTIVE', 'PARTIALLY_FULFILLED')
""")

V08 = _v("V-08", "v_backorders", "Unfillable committed demand", "commitment_id", """
SELECT c.tenant_id, c.id AS commitment_id, c.item_id, i.item_number, i.display_name AS item_name,
       c.location_id, loc.code AS location_code, c.qty_committed, c.qty_fulfilled,
       c.qty_backordered, c.external_ref, c.required_by_date, c.status,
       GREATEST(CURRENT_DATE - c.required_by_date, 0) AS days_overdue
  FROM inventory_ledger.inventory_commitment c
  LEFT JOIN item_master.item i ON i.tenant_id = c.tenant_id AND i.id = c.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = c.tenant_id AND loc.id = c.location_id
 WHERE c.qty_backordered > 0
   AND c.status IN ('ACTIVE', 'PARTIALLY_FULFILLED')
""")

V09 = _v("V-09", "v_pending_fulfillment", "Committed-not-fulfilled lines", "commitment_id", """
SELECT c.tenant_id, c.id AS commitment_id, c.external_ref, c.item_id,
       i.item_number, i.display_name AS item_name,
       c.location_id, loc.code AS location_code,
       (c.qty_committed - c.qty_fulfilled) AS qty_pending,
       c.required_by_date,
       GREATEST(CURRENT_DATE - c.committed_at::date, 0) AS days_pending
  FROM inventory_ledger.inventory_commitment c
  LEFT JOIN item_master.item i ON i.tenant_id = c.tenant_id AND i.id = c.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = c.tenant_id AND loc.id = c.location_id
 WHERE c.qty_fulfilled < c.qty_committed
   AND c.status IN ('ACTIVE', 'PARTIALLY_FULFILLED')
""")

V10 = _v("V-10", "v_inventory_activity_detail", "Movements with detail breakdown", "detail_id, line_id", """
SELECT d.tenant_id, d.id AS detail_id, d.inventory_transaction_id AS transaction_id,
       d.inventory_transaction_line_id AS line_id, t.transaction_type, t.transaction_date,
       l.item_id, i.item_number, i.display_name AS item_name,
       l.location_id, loc.code AS location_code,
       d.bin_id, bin.code AS bin_code, d.lot_id, lot.lot_number, d.serial_id, sn.serial_number,
       d.inventory_status_id, st.code AS status_code, d.ownership,
       d.qty_base AS qty_delta, d.value_base AS value_delta,
       CASE WHEN d.qty_base > 0 THEN d.qty_base ELSE 0 END AS qty_positive,
       CASE WHEN d.qty_base < 0 THEN d.qty_base ELSE 0 END AS qty_negative,
       l.movement_type
  FROM inventory_ledger.inventory_transaction_detail d
  JOIN inventory_ledger.inventory_transaction_line l
    ON l.tenant_id = d.tenant_id AND l.id = d.inventory_transaction_line_id
  JOIN inventory_ledger.inventory_transaction t
    ON t.tenant_id = d.tenant_id AND t.id = d.inventory_transaction_id
  LEFT JOIN item_master.item i ON i.tenant_id = l.tenant_id AND i.id = l.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = l.tenant_id AND loc.id = l.location_id
  LEFT JOIN traceability.bin bin ON bin.tenant_id = d.tenant_id AND bin.id = d.bin_id
  LEFT JOIN traceability.lot lot ON lot.tenant_id = d.tenant_id AND lot.id = d.lot_id
  LEFT JOIN traceability.serial_number sn ON sn.tenant_id = d.tenant_id AND sn.id = d.serial_id
  LEFT JOIN traceability.inventory_status st ON st.tenant_id = d.tenant_id AND st.id = d.inventory_status_id
""")

V11 = _v("V-11", "v_inventory_turnover_inputs", "COGS + average inventory per item/period", "item_id, period_start", """
SELECT cle.tenant_id, ic.item_id, i.item_number, i.display_name AS item_name,
       date_trunc('month', cle.effective_date)::date AS period_start,
       (date_trunc('month', cle.effective_date) + interval '1 month - 1 day')::date AS period_end,
       SUM(CASE WHEN cle.value_delta < 0 THEN -cle.value_delta ELSE 0 END) AS cogs,
       SUM(cle.qty_delta) AS qty_delta,
       COUNT(*) AS entry_count
  FROM costing.cost_ledger_entry cle
  JOIN costing.item_cost ic ON ic.tenant_id = cle.tenant_id AND ic.id = cle.item_cost_id
  LEFT JOIN item_master.item i ON i.tenant_id = ic.tenant_id AND i.id = ic.item_id
 GROUP BY cle.tenant_id, ic.item_id, i.item_number, i.display_name,
          date_trunc('month', cle.effective_date)
""", materialized=True, cost_visible=True)

V12 = _v("V-12", "v_inventory_profitability_inputs", "Revenue vs COGS per item/period", "item_id, period_start", """
SELECT cle.tenant_id, ic.item_id, i.item_number, i.display_name AS item_name,
       date_trunc('month', cle.effective_date)::date AS period_start,
       0::numeric(38,12) AS revenue_base,
       SUM(CASE WHEN cle.value_delta < 0 THEN -cle.value_delta ELSE 0 END) AS cogs_base,
       SUM(cle.qty_delta) AS qty_delta
  FROM costing.cost_ledger_entry cle
  JOIN costing.item_cost ic ON ic.tenant_id = cle.tenant_id AND ic.id = cle.item_cost_id
  LEFT JOIN item_master.item i ON i.tenant_id = ic.tenant_id AND i.id = ic.item_id
 GROUP BY cle.tenant_id, ic.item_id, i.item_number, i.display_name,
          date_trunc('month', cle.effective_date)
""", materialized=True, cost_visible=True)

V13 = _v("V-13", "v_atp_current", "Latest ATP projection per item/location/bucket", "item_id, location_id, bucket_date", """
SELECT a.tenant_id, a.item_id, i.item_number, i.display_name AS item_name,
       a.location_id, loc.code AS location_code, a.method, a.bucket_date,
       a.projected_available, a.earliest_full_date, a.shortage_qty, a.shortage_date,
       a.policy_version, a.begin_qty, a.supply_qty, a.demand_qty, a.last_rebuilt_at
  FROM replenishment.atp_projection a
  LEFT JOIN item_master.item i ON i.tenant_id = a.tenant_id AND i.id = a.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = a.tenant_id AND loc.id = a.location_id
""", fallback_sql="""
SELECT NULL::uuid AS tenant_id, NULL::uuid AS item_id, NULL::text AS item_number,
       NULL::text AS item_name, NULL::uuid AS location_id, NULL::text AS location_code,
       NULL::text AS method, NULL::date AS bucket_date, NULL::numeric AS projected_available,
       NULL::date AS earliest_full_date, NULL::numeric AS shortage_qty, NULL::date AS shortage_date,
       NULL::text AS policy_version, NULL::numeric AS begin_qty, NULL::numeric AS supply_qty,
       NULL::numeric AS demand_qty, NULL::timestamptz AS last_rebuilt_at
 WHERE false""")

V14 = _v("V-14", "v_planning_workbench", "Suggestions + action messages by plan", "supply_plan_id, item_id", """
SELECT s.tenant_id, s.supply_plan_id, s.id AS suggestion_id, s.item_id,
       i.item_number, i.display_name AS item_name,
       s.location_id, loc.code AS location_code, s.action_type, s.qty, s.due_date,
       s.status, s.priority, s.source_rule_id, s.created_doc_type, s.created_doc_id,
       m.id AS action_message_id, m.message_type, m.severity, m.message AS action_message
  FROM replenishment.supply_plan_suggestion s
  LEFT JOIN replenishment.planning_action_message m
    ON m.tenant_id = s.tenant_id AND m.supply_plan_suggestion_id = s.id
  LEFT JOIN item_master.item i ON i.tenant_id = s.tenant_id AND i.id = s.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = s.tenant_id AND loc.id = s.location_id
""", fallback_sql="""
SELECT NULL::uuid AS tenant_id, NULL::uuid AS supply_plan_id, NULL::uuid AS suggestion_id,
       NULL::uuid AS item_id, NULL::text AS item_number, NULL::text AS item_name,
       NULL::uuid AS location_id, NULL::text AS location_code, NULL::text AS action_type,
       NULL::numeric AS qty, NULL::date AS due_date, NULL::text AS status, NULL::integer AS priority,
       NULL::uuid AS source_rule_id, NULL::text AS created_doc_type, NULL::uuid AS created_doc_id,
       NULL::uuid AS action_message_id, NULL::text AS message_type, NULL::text AS severity,
       NULL::text AS action_message
 WHERE false""")

V15 = _v("V-15", "v_purchase_history", "PO/receipt/bill per item/vendor", "vendor_id, item_id, purchase_order_id", """
SELECT po.tenant_id, po.vendor_id, v.name AS vendor_name,
       pol.item_id, i.item_number, i.display_name AS item_name,
       po.id AS purchase_order_id, po.document_number, po.transaction_date,
       pol.qty_ordered,
       COALESCE(r.qty_received, 0) AS qty_received,
       COALESCE(b.qty_billed, 0) AS qty_billed,
       COALESCE(r.amount_received, 0) AS amount_received_base,
       COALESCE(b.amount_billed, 0) AS amount_billed_base,
       pol.unit_price, po.currency_code
  FROM procurement.purchase_order po
  JOIN procurement.purchase_order_line pol
    ON pol.tenant_id = po.tenant_id AND pol.purchase_order_id = po.id
  LEFT JOIN procurement.vendor v ON v.tenant_id = po.tenant_id AND v.id = po.vendor_id
  LEFT JOIN item_master.item i ON i.tenant_id = pol.tenant_id AND i.id = pol.item_id
  LEFT JOIN LATERAL (
      SELECT SUM(irl.qty_received) AS qty_received,
             SUM(irl.qty_received * irl.unit_cost * irl.exchange_rate) AS amount_received
        FROM procurement.item_receipt_line irl
        JOIN procurement.item_receipt ir
          ON ir.tenant_id = irl.tenant_id AND ir.id = irl.item_receipt_id
       WHERE irl.tenant_id = pol.tenant_id AND irl.purchase_order_line_id = pol.id
         AND ir.status = 'POSTED'
  ) r ON TRUE
  LEFT JOIN LATERAL (
      SELECT SUM(vbl.qty) AS qty_billed,
             SUM(vbl.amount * vb.exchange_rate) AS amount_billed
        FROM procurement.vendor_bill_line vbl
        JOIN procurement.vendor_bill vb
          ON vb.tenant_id = vbl.tenant_id AND vb.id = vbl.vendor_bill_id
       WHERE vbl.tenant_id = pol.tenant_id AND vbl.purchase_order_line_id = pol.id
         AND vb.status IN ('OPEN', 'PAID')
  ) b ON TRUE
""")

V16 = _v("V-16", "v_manufacturing_workbench", "Open WOs with shortages and WIP balance", "work_order_id", """
SELECT wo.tenant_id, wo.id AS work_order_id, wo.document_number,
       wo.assembly_item_id, ai.item_number AS assembly_item_number,
       ai.display_name AS assembly_item_name, wo.wo_type, wo.status,
       wo.qty_ordered, wo.qty_built, wo.qty_scrapped, wo.uom_id,
       wo.start_date, wo.end_date,
       COALESCE(short.shortage_qty, 0) AS shortage_qty,
       wb.wip_value, wb.qty_issued, wb.qty_completed
  FROM manufacturing.work_order wo
  LEFT JOIN item_master.item ai ON ai.tenant_id = wo.tenant_id AND ai.id = wo.assembly_item_id
  LEFT JOIN manufacturing.work_order_wip_balance wb
    ON wb.tenant_id = wo.tenant_id AND wb.work_order_id = wo.id
  LEFT JOIN LATERAL (
      SELECT SUM(GREATEST(woc.qty_required - woc.qty_issued, 0)) AS shortage_qty
        FROM manufacturing.work_order_component woc
        JOIN manufacturing.work_order_line wol
          ON wol.tenant_id = woc.tenant_id AND wol.id = woc.work_order_line_id
       WHERE wol.tenant_id = wo.tenant_id AND wol.work_order_id = wo.id
  ) short ON TRUE
 WHERE wo.status IN ('PLANNED', 'FIRM_PLANNED', 'RELEASED', 'IN_PROCESS')
""", materialized=True, fallback_sql="""
SELECT NULL::uuid AS tenant_id, NULL::uuid AS work_order_id, NULL::text AS document_number,
       NULL::uuid AS assembly_item_id, NULL::text AS assembly_item_number,
       NULL::text AS assembly_item_name, NULL::text AS wo_type, NULL::text AS status,
       NULL::numeric AS qty_ordered, NULL::numeric AS qty_built, NULL::numeric AS qty_scrapped,
       NULL::uuid AS uom_id, NULL::date AS start_date, NULL::date AS end_date,
       NULL::numeric AS shortage_qty, NULL::numeric AS wip_value,
       NULL::numeric AS qty_issued, NULL::numeric AS qty_completed
 WHERE false""")

V17 = _v("V-17", "v_lot_expiry", "Lots expiring within horizon", "lot_id", """
SELECT bl.tenant_id, l.id AS lot_id, l.item_id, i.item_number, i.display_name AS item_name,
       l.lot_number, l.expiry_date, l.is_active,
       bl.location_id, loc.code AS location_code,
       bl.inventory_status_id, st.code AS status_code,
       bl.qty_on_hand, bl.qty_committed, (bl.qty_on_hand - bl.qty_committed) AS qty_available
  FROM traceability.lot l
  LEFT JOIN inventory_ledger.inventory_balance_lot bl
    ON bl.tenant_id = l.tenant_id AND bl.lot_id = l.id
  LEFT JOIN item_master.item i ON i.tenant_id = l.tenant_id AND i.id = l.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = bl.tenant_id AND loc.id = bl.location_id
  LEFT JOIN traceability.inventory_status st ON st.tenant_id = bl.tenant_id AND st.id = bl.inventory_status_id
 WHERE l.expiry_date IS NOT NULL
""")

V18 = _v("V-18", "v_serial_history", "Serial movement history", "occurred_at, history_id", """
SELECT h.tenant_id, h.id AS history_id, h.serial_id, sn.serial_number,
       sn.item_id, i.item_number, i.display_name AS item_name,
       h.inventory_transaction_id AS transaction_id, h.inventory_transaction_line_id AS line_id,
       h.event,
       h.from_location_id, fl.code AS from_location_code,
       h.from_bin_id, h.from_status_id,
       h.to_location_id, tl.code AS to_location_code,
       h.to_bin_id, h.to_status_id, h.ownership, h.customer_ref, h.occurred_at
  FROM traceability.serial_number_history h
  LEFT JOIN traceability.serial_number sn ON sn.tenant_id = h.tenant_id AND sn.id = h.serial_id
  LEFT JOIN item_master.item i ON i.tenant_id = sn.tenant_id AND i.id = sn.item_id
  LEFT JOIN traceability.location fl ON fl.tenant_id = h.tenant_id AND fl.id = h.from_location_id
  LEFT JOIN traceability.location tl ON tl.tenant_id = h.tenant_id AND tl.id = h.to_location_id
""")

V19 = _v("V-19", "v_consigned_balance", "Consigned on-hand/value by vendor/agreement", "item_id, location_id", """
SELECT b.tenant_id, ca.id AS agreement_id, ca.agreement_number, ca.vendor_id, v.name AS vendor_name,
       b.item_id, i.item_number, i.display_name AS item_name,
       b.location_id, loc.code AS location_code,
       b.qty_on_hand AS qty_consigned, b.value_base AS value_consigned,
       b.inventory_status_id, st.code AS status_code
  FROM inventory_ledger.inventory_balance b
  LEFT JOIN consigned.consignment_agreement_item cai
    ON cai.tenant_id = b.tenant_id AND cai.item_id = b.item_id AND cai.location_id = b.location_id
  LEFT JOIN consigned.consignment_agreement ca
    ON ca.tenant_id = cai.tenant_id AND ca.id = cai.consignment_agreement_id
  LEFT JOIN procurement.vendor v ON v.tenant_id = ca.tenant_id AND v.id = ca.vendor_id
  LEFT JOIN item_master.item i ON i.tenant_id = b.tenant_id AND i.id = b.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = b.tenant_id AND loc.id = b.location_id
  LEFT JOIN traceability.inventory_status st
    ON st.tenant_id = b.tenant_id AND st.id = b.inventory_status_id
 WHERE b.ownership = 'CONSIGNED'
""", fallback_sql="""
SELECT b.tenant_id, NULL::uuid AS agreement_id, NULL::text AS agreement_number,
       NULL::uuid AS vendor_id, NULL::text AS vendor_name,
       b.item_id, i.item_number, i.display_name AS item_name,
       b.location_id, loc.code AS location_code,
       b.qty_on_hand AS qty_consigned, b.value_base AS value_consigned,
       b.inventory_status_id, st.code AS status_code
  FROM inventory_ledger.inventory_balance b
  LEFT JOIN item_master.item i ON i.tenant_id = b.tenant_id AND i.id = b.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = b.tenant_id AND loc.id = b.location_id
  LEFT JOIN traceability.inventory_status st
    ON st.tenant_id = b.tenant_id AND st.id = b.inventory_status_id
 WHERE b.ownership = 'CONSIGNED'""")

V20 = _v("V-20", "v_quality_inspection_status", "Open inspections/results/dispositions", "inspection_id", """
SELECT q.tenant_id, q.id AS inspection_id, q.inspection_number, q.item_id,
       i.item_number, i.display_name AS item_name,
       q.location_id, loc.code AS location_code,
       q.source_entity_type, q.source_entity_id, q.status, q.disposition,
       q.inspector_id, q.started_at, q.completed_at, q.result_summary,
       (SELECT COUNT(*) FROM quality.inspection_result r
         WHERE r.tenant_id = q.tenant_id AND r.inspection_id = q.id) AS result_count
  FROM quality.inspection q
  LEFT JOIN item_master.item i ON i.tenant_id = q.tenant_id AND i.id = q.item_id
  LEFT JOIN traceability.location loc ON loc.tenant_id = q.tenant_id AND loc.id = q.location_id
""", fallback_sql="""
SELECT NULL::uuid AS tenant_id, NULL::uuid AS inspection_id, NULL::text AS inspection_number,
       NULL::uuid AS item_id, NULL::text AS item_number, NULL::text AS item_name,
       NULL::uuid AS location_id, NULL::text AS location_code, NULL::text AS source_entity_type,
       NULL::uuid AS source_entity_id, NULL::text AS status, NULL::text AS disposition,
       NULL::uuid AS inspector_id, NULL::timestamptz AS started_at,
       NULL::timestamptz AS completed_at, NULL::jsonb AS result_summary,
       NULL::bigint AS result_count
 WHERE false""")

V21 = _v("V-21", "v_available_to_build", "Pass-through of FS-09 atb-v1 available-to-build", "assembly_item, location_id", """
SELECT atb.tenant_id, atb.assembly_item, atb.location_id, atb.level_mode, atb.basis, atb.as_of,
       atb.buildable_qty, atb.limiting_component, atb.per_level, atb.policy_version, atb.input_hash,
       atb.qty_per_component, atb.available_component_qty, atb.degraded
  FROM manufacturing.available_to_build_cache atb
""", materialized=True, fallback_sql="""
SELECT NULL::uuid AS tenant_id, NULL::uuid AS assembly_item, NULL::uuid AS location_id,
       NULL::text AS level_mode, NULL::text AS basis, NULL::timestamptz AS as_of,
       NULL::numeric AS buildable_qty, NULL::uuid AS limiting_component, NULL::jsonb AS per_level,
       NULL::text AS policy_version, NULL::text AS input_hash,
       NULL::numeric AS qty_per_component, NULL::numeric AS available_component_qty,
       NULL::boolean AS degraded
 WHERE false""")

VIEWS: tuple[ViewSpec, ...] = (V01, V02, V03, V04, V05, V06, V07, V08, V09, V10, V11, V12,
                               V13, V14, V15, V16, V17, V18, V19, V20, V21)
VIEW_NAMES: tuple[str, ...] = tuple(v.name for v in VIEWS)
MATERIALIZED_VIEWS = tuple(v.name for v in VIEWS if v.materialized)

__all__ = ["VIEWS", "VIEW_NAMES", "MATERIALIZED_VIEWS", "ViewSpec"]
