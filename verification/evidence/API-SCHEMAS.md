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


## Response, error and security contract (final phase)

Implemented in `app/api/response_schemas.py` and applied during `app.openapi()` generation.

| Contract area | Coverage |
|---|---:|
| 2xx JSON responses | 1,244 / 1,244 typed |
| Opaque 2xx JSON responses | **0** |
| Request bodies | 714 / 714 typed |
| Opaque request bodies | **0** |
| Operations with 400/401/403/404/409/422/500 `application/problem+json` | 1,259 / 1,259 |
| Problem+json error response definitions | 8,813 |
| Security schemes | BearerAuth, ApiKeyAuth, SuperAdminToken |
| Global security | Bearer JWT or tenant/OpenAPI API key |
| Public/auth overrides | login, select-tenant, providers, SSO, invitation accept, health/readiness |
| MCP endpoint | ApiKeyAuth only |
| Bootstrap endpoints | SuperAdminToken |
| Report operations | 22 |

The response layer uses shared typed envelopes for:

- list responses: `items[]`, `count`, `next_cursor`
- detail/create/update: resource object with core fields plus documented domain fields
- lifecycle actions: typed action result
- reports: typed report envelope
- health/readiness/OpenAPI/MCP-specific shapes
- problem+json errors

`test_openapi_contract_is_complete` now fails if any request body, 2xx JSON response, problem+json error,
security scheme, or report-route count regresses.

### Contract caveat

The response object is typed, but nested report rows and some variable-shape extension payloads are
intentionally open objects because their columns depend on the report/consumer configuration. All
first-level API success and error shapes are constrained.


## Native boundary DTO migration (final)

The request/response schema-injection fallback has been removed. Every mounted route now uses
FastAPI-native Pydantic boundary models.

- 646/646 JSON request bodies are named component schemas
- 1,244/1,244 JSON 2xx responses are typed (0 opaque)
- 0 operations use `x-schema-source` injection
- 1,328 tests passing; 204/204 E2E checks; 1,259 operations with 0 × 5xx
