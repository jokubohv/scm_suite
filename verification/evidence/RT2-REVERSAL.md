# RT2 — receipt reversal 500 root cause and fix

## Root cause

Production traceback showed:

```text
inventory_ledger.inventory_transaction_detail insert
TN003: lot/serial detail is required for a numbered item
```

`LedgerService.reverse()` rebuilt reversal movements from `inventory_transaction_line` only.
For a receipt of a numbered item:

- line `lot_id` was NULL
- lot/serial dimensions existed only in `inventory_transaction_detail`

The reversal therefore produced a null-lot detail and the DB trigger rejected it. The exception was
unhandled, producing `500 INTERNAL_ERROR` with no correlation.

## Fix

- `LedgerService.reverse()` now loads original `inventory_transaction_detail` rows, groups by line, and
  emits negated detail rows in each reversal movement. This preserves lot/serial/bin/status/ownership
  through the reversal and satisfies the numbered-item trigger.
- `RequestContextMiddleware` now stores correlation/trace on `request.state`.
- `problem_response()` falls back to `request.state`/request headers and returns `X-Request-ID` /
  `X-Trace-ID` on error responses.

## Regression coverage

- `test_lot_transaction_reverse_preserves_details`: T-01 with `details[].lot_id` → reverse → 200,
  lot balance returns to zero.
- `test_internal_error_has_correlation_and_trace`: unhandled error → 500 problem+json with
  correlation_id/trace_id and response headers.
- Existing AVERAGE/FIFO receipt post→reverse probes remain.

## Test performance

`pytest-xdist` is now a dev dependency and default:

```toml
addopts = "-q -n auto --dist loadfile"
```

Full suite on this machine dropped from ~7m19s serial to **~1m44s** parallel.
