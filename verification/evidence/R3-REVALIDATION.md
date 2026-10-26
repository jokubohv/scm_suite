# R3 contract revalidation — remediation status

## F1 — wrapped `lines[]` request bodies

**Fixed.** The root cause was `_lines()` receiving an already-built line object schema and wrapping it again.
Malformed `lines[].properties.type/properties` schemas: **45 → 0**. Verified on receipts, adjustments,
fulfillments, inventory transactions and transfers.

## F3 — router-typed bodies overwritten

**Fixed.** Schema injection now only fills missing/opaque bodies. It dereferences `$ref` components and
does not overwrite typed bodies, including `anyOf[Model, null]` bodies such as count approve.
Router-typed bodies overwritten: **0** by local source audit.

## F1/F2 — P1/P2 blockers

- count capture `/inventory-counts/{id}/lines` exposes `client_batch_id` + `lines[]`
- count recount exposes `client_batch_id`, `lines[]`, `round`
- count approve exposes `comment`, `approved_by`
- count reject exposes `reason`, `recalc_snapshot`, `approved_by`, `comment`
- count recalculate-snapshot retains its router schema
- commitment release exposes required `qty` and `reason`
- receipt type accepts `STANDALONE`; `vendor_id` is no longer unconditionally required
- commitment create documents `source_line_ref`
- MCP count capture tool added: `inventory_counts_lines`

## F2 — remaining generic ACTION bodies

**Partially fixed.** Bodyless routes no longer get invented request bodies, and all core blockers above
are now router-typed/specific. However **160 optional generic ACTION request bodies remain** for
secondary lifecycle endpoints whose routers intentionally accept free-form optional dictionaries
(activate/suspend/void/etc.). These are no longer P1/P2 blockers, but they are not source-specific yet.

## D9 — response fidelity

**Partially fixed.**
- malformed embedded `lines[]` responses: **110 → 0**
- GET fallback no longer uses an action envelope; unknown GETs get read/list envelopes
- known resource actions return resource+action fields rather than a bare action envelope
- report envelope includes `as_of`, `generated_at`, `refreshed_at` and `freshness`
- report operations: 22 (20 concrete + catalogue + export; no `/reports/search` routes)

**Remaining:** resource response objects are still synthesized from request schemas rather than actual
response DTOs, and 160 optional ACTION request bodies remain. Independent revalidation should continue
to treat response fidelity as incomplete.

## D10 — auth and errors

**Fixed.**
- security misattributions: **0**
- public endpoints: `security: []`
- self-service `/auth/*`, `/me*`, `/platform/*`: Bearer only
- `/mcp`: API key only
- `/openapi.json`: API key only
- bootstrap: super-admin token
- 412 and 503 problem+json responses added to every operation

## MCP

- tools: **246**
- generic-body MCP tools: **0**
- wrapped-line MCP tools: **0**
- count capture, recount, approve, reject, post, recalculate-snapshot and commitment release present
- API-key-only enforcement unchanged

## Verification

- full suite: **1,321 passed**
- local E2E: **204 checks, 0 failures**
- surface smoke: **1,259 operations, 0 × 5xx**
- problem+json missing operations: **0**
- security misattributions: **0**
