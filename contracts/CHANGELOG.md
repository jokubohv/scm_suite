# Contract changelog

All provided contracts are semver'd; changes are additive within a major version (framework §5).

## 1.5.0 — S0-04 contract-first scaffold (2026-10-23)

- Initial generated skeleton: 16 feature-set OpenAPI fragments, aggregated OpenAPI 3.1 document,
  21 provided-contract registry entries, event JSON-Schema
  stubs with the final outbox envelope, builder extension points under `app/contracts/<dir>/`.
- No contract is frozen yet: freeze points C0–C5 in `design/parallel-plan.json` apply at their
  feature-set gates. Skeleton operations are not a compatibility promise.
