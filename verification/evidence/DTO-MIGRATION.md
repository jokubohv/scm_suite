# FastAPI DTO migration — P1 slice

## Architecture

P1 routes now use native FastAPI boundary models:

- typed Pydantic request bodies
- `response_model=...` on route decorators
- automatic OpenAPI schemas from those models
- runtime response validation
- no schema injection for these routes

## Migrated routes

- `GET/POST /api/v1/items`
- `GET/PATCH /api/v1/items/{item_id}`
- `GET/POST /api/v1/item-receipts`
- `GET/PATCH /api/v1/item-receipts/{receipt_id}`
- `POST /api/v1/item-receipts/{receipt_id}/post`
- `POST /api/v1/item-receipts/{receipt_id}/reverse`
- `GET/POST /api/v1/inventory-adjustments`
- `GET /api/v1/inventory-adjustments/{id}`
- adjustment submit/post/reverse/void actions
- `GET/POST /api/v1/inventory-transfers`
- `GET /api/v1/inventory-transfers/{id}`
- transfer submit/post/reverse/void actions
- `GET/POST /api/v1/inventory-counts`
- `GET /api/v1/inventory-counts/{count_id}`
- count start/submit/approve/reject/recalculate-snapshot/post actions

## Models

- `app/api/schemas/common.py`: `APIModel`, `Problem`, `Page[T]`, `ActionResult`
- `app/api/schemas/items.py`: item summary/detail/inventory/UOM/request models
- `app/api/schemas/procurement.py`: receipt summary/detail/line/request/action models
- `app/api/schemas/inventory.py`: adjustment/transfer DTOs and request models
- `app/api/schemas/counts.py`: count/scope/line/action DTOs

## Verified

- full suite passes after artifact regeneration
- local E2E: 204 checks, 0 failures
- surface sweep: 1,259 operations, 0 × 5xx
- OpenAPI contains native `$ref` response models for the migrated routes

## Not migrated yet

All other domains still use the temporary request/response schema injection layer:

- procurement POs/bills beyond receipts
- WMS
- manufacturing
- quality
- planning
- reports
- auth/admin/platform
- remaining inventory modules

The injection layer remains only as a compatibility fallback until those routes are migrated.
