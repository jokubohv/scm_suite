# API request-schema coverage

## Before

- Runtime operations: 1,259
- Operations with a request body: 647
- Opaque request bodies (`additionalProperties: true`, no fields): **360**
- Curated MCP tools: 240
- MCP tools with a body: 134
- **Opaque MCP body tools: 113**

## Phase 1 (implemented)

Central registry: `app/api/request_schemas.py`, injected into `app.openapi()` during app construction.

Covered:
- items and master data: items, locations, bins, statuses, categories, UOMs, UOM conversions
- pricing: price levels, item prices
- inventory: adjustments, transfers, counts, count-plan updates
- procurement: vendors, POs, requests, requisitions, RFQs, contracts, receipts, bills, credits, returns
- WMS: warehouses, inbound shipments, receiving orders, putaway tasks, waves, pick tasks, pack tasks, shipments
- manufacturing: assemblies, BOMs, routings, work centers, work orders
- quality: specs, inspection plans, inspections
- consignment, warranty registrations, UPC registry, allocation runs

## After Phase 1

- Runtime operations with a request body: 682 (action schemas now documented too)
- Opaque request bodies: **171**
- **Opaque MCP body tools: 0 / 151**
- MCP write tools now expose real fields, required flags, enums and nested line schemas.

Example `item_receipts_create` input schema now includes:

`purchase_order_id`, `vendor_id`, `receipt_type`, `received_at`, `location_id`, `memo`,
`lines[]` with `purchase_order_line_id`, `item_id`, `qty_received`, `unit_cost`, `location_id`,
`inventory_status_id`, `bin_id`, `lot_id`, `serial_ids`, `ownership`, `details[]`.

## Remaining Phase 2 work

171 opaque operations remain outside the current MCP catalogue. They are concentrated in:

- pricing schedules/rules/collections/coupons/price updates
- item sub-resources (custom fields, purchasing/sales/accounting, barcodes, matrix, kit)
- transfer orders, worksheets, distributions, commitments, imports, reason codes
- costing templates, landed costs, location costing groups, recalculations
- WMS setup resources, strategies, bin blocks, wave schedules, ship manifests, replenishment
- quality queues/triggers/COAs
- planning rules, time fences, DRP networks
- warranty claims/repair orders, customer parts, consignment sub-resources
- the MCP JSON-RPC endpoint itself (intentionally free-form JSON-RPC)

The registry is the single place to add Phase 2 schemas; MCP will inherit them automatically.
