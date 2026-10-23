"""Shared test utilities for the inventory-platform backend (S0-05).

Public surface (stable for feature-set test suites):

* :mod:`app.tests.utils.deterministic` — fixed clock and deterministic UUIDv7 helpers
* :mod:`app.tests.utils.factories` — tenant/principal/item/location/status/posting factories
* :mod:`app.tests.utils.assertions` — decimal/UUID/envelope assertion helpers
* :mod:`app.tests.utils.invariants` — invariant harness base and generic ledger invariants
* :mod:`app.tests.utils.golden` — golden-file load/compare/update helpers
* :mod:`app.tests.utils.db` — DB profile resolution (URL / testcontainers / compose / graceful skip)
* :mod:`app.tests.utils.context` — tenant/principal request-context placeholders
"""

from app.tests.utils.assertions import (
    as_decimal,
    assert_decimal_equal,
    assert_error_envelope,
    assert_money_string,
    assert_quantity_string,
    assert_scope_denied,
    assert_utc_timestamp,
)
from app.tests.utils.deterministic import FIXED_EPOCH, DeterministicUUIDs, FixedClock, uuid7_at
from app.tests.utils.factories import (
    Identity,
    ItemSample,
    LedgerPostingSample,
    LocationSample,
    Principal,
    RoleSample,
    Tenant,
    UomPairSample,
    make_identity,
    make_item,
    make_ledger_posting,
    make_location,
    make_principal,
    make_role,
    make_status,
    make_tenant,
    make_uom_pair,
    scopes_for_role,
)
from app.tests.utils.golden import assert_matches_golden, load_golden, write_golden
from app.tests.utils.invariants import InvariantHarness, InvariantReport, InvariantResult

__all__ = [
    "FIXED_EPOCH",
    "DeterministicUUIDs",
    "FixedClock",
    "uuid7_at",
    "InvariantHarness",
    "InvariantReport",
    "InvariantResult",
    "Identity",
    "ItemSample",
    "LedgerPostingSample",
    "LocationSample",
    "Principal",
    "RoleSample",
    "Tenant",
    "UomPairSample",
    "as_decimal",
    "assert_decimal_equal",
    "assert_error_envelope",
    "assert_matches_golden",
    "assert_money_string",
    "assert_quantity_string",
    "assert_scope_denied",
    "assert_utc_timestamp",
    "load_golden",
    "make_identity",
    "make_item",
    "make_ledger_posting",
    "make_location",
    "make_principal",
    "make_role",
    "make_status",
    "make_tenant",
    "make_uom_pair",
    "scopes_for_role",
    "write_golden",
]
