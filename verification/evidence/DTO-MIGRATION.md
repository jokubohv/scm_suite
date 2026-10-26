# FastAPI-native DTO migration — complete

All mounted API routes now use FastAPI route-boundary Pydantic request/response models.
The request-schema injection step has been removed from `app.openapi()` generation.

## Coverage

| Metric | Result |
|---|---:|
| Runtime paths | 982 |
| Runtime operations | 1,259 |
| JSON request bodies | 646 |
| Request bodies with named schemas | **646 / 646** |
| Opaque request bodies | **0** |
| JSON 2xx responses | 1,244 |
| Typed JSON 2xx responses | **1,244 / 1,244** |
| Opaque 2xx responses | **0** |
| Operations still using request-schema injection | **0** |
| MCP tools | 246 |

The 36 non-`$ref` JSON responses use valid inline/union schemas (for example auth login's
`TokenPairResponse | SelectionRequiredResponse`); none are unconstrained objects.

## Migrated domains

- **FS-00 platform**: auth, bootstrap, RBAC, API keys, artifacts, config, extras, ops, approvals,
  platform admin, boundary, SSO, MCP
- **FS-01 items**: all 62 item-master routes
- **FS-03/06 traceability**: locations, zones, bins, statuses, lots, serials, numbered inventory
- **FS-04 inventory ledger**: transactions, adjustments, worksheets, transfers, transfer orders,
  commitments, fulfilments/returns, balances/availability, distributions, imports, reason codes
- **FS-07 counts**: count plans, counts, capture/recount/approve/reject/post
- **FS-08 procurement**: vendors, receipts, POs, bills, contracts, requisitions, RFQs, returns,
  credits, landed costs, special-order links
- **FS-09 manufacturing**: assemblies, BOMs, routings, work centers, work orders, WIP, builds/unbuilds,
  scheduling, outsourced operations, ECOs, available-to-build
- **FS-10 replenishment/planning**
- **FS-11 WMS**
- **FS-12 quality**
- **FS-13 consignment**
- **FS-14 extensions**
- **FS-15 reporting** with typed freshness envelopes

## Intentional no-body responses

- 204 DELETE routes have no response body and therefore no response model.
- `GET /api/v1/reports/{report}/export` returns a file/binary `Response` by design.
- `POST /api/v1/mcp` is a raw JSON-RPC response by protocol; its request body is typed
  `JSONRPCRequest`.

## Verification

- Full suite: **1,328 passed**
- Local E2E: **204 checks, 0 failures**
- Surface sweep: **1,259 operations, 0 × 5xx**
- Runtime OpenAPI artifact regenerated
- Contract artifacts regenerated
