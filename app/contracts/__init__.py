"""Contract-first infrastructure (S0-04).

Deterministic, stdlib-only tooling that parses the feature-set implementation specs
(`feature-sets/FS-*.md`) and the manifest (`design/feature-sets.json`) into committed
contract artefacts:

* `contracts/openapi/fragments/FS-*.openapi.json` — per-feature-set OpenAPI skeletons
* `contracts/openapi/openapi.json|yaml` — aggregated OpenAPI 3.1 document
* `contracts/openapi/contracts.json` — contract registry (21 provided contracts)
* `contracts/events/registry.json|yaml` + `contracts/events/<contract>/<event>.schema.json`
* `app/contracts/<dir>/contract.json|openapi.fragment.json|README.md` — builder extension points

Usage::

    python -m app.contracts.cli generate   # write/refresh committed artefacts
    python -m app.contracts.cli lint       # structural + freshness checks (CI gate)
    python -m app.contracts.cli diff --baseline old.json --current new.json
    python -m app.contracts.cli list
"""

__version__ = "1.0.0"
