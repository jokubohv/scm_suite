# API request-schema coverage

## Final status: 0 opaque request bodies

The runtime API uses a central request-schema registry (`app/api/request_schemas.py`) injected into
`app.openapi()` during app construction. MCP tool input schemas are generated from that same document,
so they inherit the typed request bodies automatically.

## Before

| Measure | Count |
|---|---:|
| Runtime operations | 1,259 |
| Operations with a request body | 647 |
| **Opaque request bodies** (`additionalProperties: true`, no fields) | **360** |
| Curated MCP tools | 240 |
| MCP body tools | 134 |
| **Opaque MCP body tools** | **113** |

## After

| Measure | Count |
|---|---:|
| Runtime operations | 1,259 |
| Operations with a request body | 700 |
| **Opaque request bodies** | **0** |
| MCP tools | 240 |
| MCP body tools | 151 |
| **Opaque MCP body tools** | **0** |

The extra 53 body operations are action endpoints that previously had no documented request body;
they now document their optional action fields.

## Covered domains

- items and item sub-resources: type extensions, custom fields, UOM roles, location planning,
  barcodes, matrix generation/linking, kit members, warranty
- items/master data: items, locations, bins, statuses, categories, UOMs, UOM types, conversions
- pricing: levels, item prices, schedules, breaks, rules/criteria/actions, groups, collections,
  coupons, mass price updates, level swaps, purchase-price updates, price lists
- inventory: balances, adjustments, transfers, transfer orders, counts, count plans, worksheets,
  commitments, distributions, imports, reason codes, fulfillments, returns
- procurement: vendors, vendor categories, item vendors/prices, POs, requests, requisitions, RFQs,
  contracts, receipts, bills, credits, returns, special-order links
- costing: cost templates, recalculation, COGS adjustments, landed costs, estimated templates,
  location costing groups
- WMS: warehouses, inbound, receiving, putaway, waves, pick/pack, shipments, manifests, zones,
  bin blocks, strategies, wave schedules, replenishment, carts, device sessions, status tasks
- manufacturing: assemblies, BOMs, routings, work centers, work orders
- quality: specs, plans, inspections, queues, triggers, COAs, settings
- consignment: agreements, transfers, consumption, settings
- extensions: warranty registrations/claims/repair orders, customer parts, UPC registry
- planning: allocation runs, planning rules, time fences, DRP networks
- platform/integration: MCP JSON-RPC envelope

## Verification

- Test gate: no POST/PUT/PATCH request body anywhere in `app.openapi()` may have an opaque schema
- MCP gate: no body-bearing MCP tool may have an empty/opaque `inputSchema`
- Representative schema assertions for receipts, adjustments, items and POs
- Full suite: **1,320 passed**
- Local E2E: **204 checks, 0 failures**
- Surface smoke: **1,259 operations, 0 × 5xx**

## Runtime validation

The registry fixes the documented API/MCP contract. Service-layer validation remains the
authoritative runtime validator as before; boundary-level enforcement of these schemas is a separate
follow-up (recommended next).
