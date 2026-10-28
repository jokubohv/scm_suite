# Invoice matching — mainline port checkpoint

Ready for review. Branch `codex/invoice-matching-mainline` is based on the remote default mainline `origin/master` at **2dbdda9f0b4830c498733a599d12448c2b42309b**. The remote has no `main` branch. This is the delivery candidate requested by the user; the earlier `codex/invoice-catalog-matching` checkout based on12448db remains intact as superseded evidence. The dirty primary SCM costing workspace was not changed. No commit, push, deployment, migration, global dependency change or running-runtime replacement occurred.

## Scope

Only three production files change:

- Procurement service: vendor continuation uses tenant-scoped cursor-row lookup and `(vendor_number,id)` ordering/predicate with one extra row to determine continuation; exact `tax_id` and `vendor_number` filters; item-vendor exact `vendor_item_code` and complete `(item_id,vendor_id,id)` continuation. Existing filters and tenant scope are retained.
- Procurement router: additive bounded matching filters and item-vendor cursor/page size, with existing domain error mapping.
- Procurement schemas: `matching_contract_version: 1` on vendor and item-vendor pages. Clients can distinguish qualified exact filtering from older services that ignore unknown query parameters.

The only other tracked changes are regenerated procurement builder/FS-08 fragment, aggregate JSON/YAML and runtime OpenAPI. Mainline has no web project or generated web API file; by manager decision this port does not introduce an orphan web directory. Its authoritative OpenAPI artifacts are available to consumers.

## Test-first evidence

The previously qualified real router/auth/PostgreSQL regression was copied unchanged from the superseded checkout. Before production edits, `scm-mainline-matching-attempt-1.log` reached the behavioral expected red: vendor pagination repeated/omitted records with deliberately noncorrelated UUID/order. The fixture mechanism succeeded and tenant/vendor counts were0 before and0 after rollback. Manager inspected the red before authorizing the port.

`scm-mainline-matching-attempt-2.log`: **one focused integration regression passed**. It traverses the actual HTTP/router/auth/PostgreSQL boundary and verifies complete vendor pages, exact tax/number results, ambiguity across supplier-SKU pages, exclusion of near matches, and tenant isolation. There are no response mocks, stock/receipt writes, or retained fixtures. The before/after counts remain0→0. The reused pytest-asyncio harness emits its existing fixture-loop-scope deprecation warning; it does not alter results.

The Serebo task owns the guarded runner and database fixture configuration. From that task's root:

```
python3 infra/local/run/iui-full-20261027/run-scm-mainline-matching-check.py
```

The independent runner preserves the old runner and old evidence. It mounts this checkout read-only into the pinned native task image, validates the owned Docker label, database/role identity and exact target, and checks rollback counts. For another explicitly owned migrated test database, the underlying test command is:

```
TEST_DB_PROFILE=url TEST_DATABASE_URL=<owned test DSN> python -m pytest app/tests/integration/procurement/test_invoice_catalog_matching.py -n 0 -q
```

## Contract evidence against this mainline

`baseline.json` and `contracts-baseline.log` record a generated pre-edit mainline OpenAPI using the project's exact FastAPI0.138.0 and Pydantic2.13.4. That generated document exactly matched the committed baseline; there was no stale baseline artifact to normalize away.

`run-contracts.py` uses the already-installed task-local pinned dependencies in an offline temporary Docker process. It regenerates runtime, procurement builder fragment and aggregate, runs contract lint, then compares full operation sets and scoped changes against this mainline commit. Reproduce from this checkout:

```
python3 verification/invoice-matching/run-contracts.py
```

Results in `contracts.json` / `contracts-generate.log`:

| Surface | Paths | Operations | Added / removed |
|---|---:|---:|---:|
| Mainline runtime before and after | 982 | 1259 | 0 / 0 |
| Mainline aggregate before and after | 991 | 1268 | 0 / 0 |

Contract lint: **passed,0 errors,0 warnings**. Runtime/aggregate operation changes are confined to GET `/api/v1/vendors` and GET `/api/v1/item-vendors`. Runtime schema changes are confined to `VendorPage` and `ItemVendorPage`. The script asserts the exact eight tracked changed files, preventing unrelated generator drift. `git diff --check` passes.

## Qualified-runtime relationship and boundary

`qualified-method-comparison.json` records AST equality (excluding line/column metadata) of both `ProcurementService.list_vendors` and `list_item_vendors` against the superseded, already-qualified fix. **Both are identical.** This includes the composite vendor paging behavior missing from master. The current synthetic SCM runtime overlay was deliberately left unchanged while the joined invoice browser test was active; no whole-runtime equivalence is claimed. The active costing workspace and its extra endpoints are not part of this mainline port.

These are local functional/contract checks, not a deployment or Linux/AMD64 packaged runtime qualification. A later release must use this requested mainline candidate and preserve the ordinary deployment/review gates.
