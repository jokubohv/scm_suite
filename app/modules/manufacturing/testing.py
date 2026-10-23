"""Deterministic FS-09 test doubles: captured posting/balance ports and seed helpers.

No live ledger/costing required: integration tests exercise manufacturing document/state/read-model
behaviour against the real Postgres schema with an injected recording ``PostingPort``; the frozen FS-04
engine is covered by its own exit suites. Pure helpers in this module are also used by invariant tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any


class FakePostingPort:
    """Recording stand-in for FS-04 ``PostingPort`` (returns deterministic transaction ids)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reversals: list[dict[str, Any]] = []
        self.commitments: list[dict[str, Any]] = []

    def _record(self, operation: str, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        transaction_id = str(uuid.uuid4())
        payload = {
            "transaction_id": transaction_id,
            "operation": operation,
            "posting_type": {
                "post_wip_issue": "T-17",
                "post_wip_completion": "T-18",
                "post_wip_close": "T-19",
                "post_assembly_build": "T-15",
                "post_assembly_unbuild": "T-16",
            }.get(operation),
            **dict(kwargs),
        }
        self.calls.append(payload)
        return payload

    async def post_wip_issue(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("post_wip_issue", kwargs)

    async def post_wip_completion(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("post_wip_completion", kwargs)

    async def post_wip_close(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("post_wip_close", kwargs)

    async def post_assembly_build(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("post_assembly_build", kwargs)

    async def post_assembly_unbuild(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("post_assembly_unbuild", kwargs)

    async def post(self, command: Mapping[str, Any]) -> dict[str, Any]:
        """Generic `PostingPort.post` used by explicit WIP transaction posting (T-17/T-18/T-19)."""
        return self._record("post", dict(command))

    async def commit(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        commitment_id = str(uuid.uuid4())
        self.commitments.append({**dict(payload), "commitment_id": commitment_id, "idempotency_key": idempotency_key})
        return {"commitment_id": commitment_id}

    async def reverse(self, transaction_id: str, *, reason: str, idempotency_key: str) -> dict[str, Any]:
        self.reversals.append({"transaction_id": transaction_id, "reason": reason, "idempotency_key": idempotency_key})
        return {"reversal_transaction_id": str(uuid.uuid4())}

    def movements(self, operation: str) -> list[Mapping[str, Any]]:
        return [call["movements"] for call in self.calls if call["operation"] == operation]


class FakeBalancePort:
    """Deterministic ``BalanceReadPort`` (item/location -> on_hand/available)."""

    def __init__(self, balances: Mapping[tuple[str, str], Mapping[str, Any]] | None = None) -> None:
        self.balances = {key: dict(value) for key, value in (balances or {}).items()}

    def set(self, item_id: str, location_id: str, *, on_hand: Any = 0, available: Any = None) -> None:
        self.balances[(str(item_id), str(location_id))] = {
            "on_hand": str(on_hand),
            "available": str(available if available is not None else on_hand),
        }

    async def availability(
        self, *, item_id: str, location_id: str, inventory_status_id: str | None = None
    ) -> dict[str, Any]:
        stored = self.balances.get((str(item_id), str(location_id)))
        if stored is None:
            return {"on_hand": "0", "available": "0"}
        return dict(stored)


def signed_qty(value: Any) -> Decimal:
    return Decimal(str(value))


def movement_totals(movements: Sequence[Mapping[str, Any]]) -> dict[str, Decimal]:
    """Quantity conservation helper (INV-01): signed movements balance per posting plan."""
    totals: dict[str, Decimal] = {}
    for movement in movements:
        item = str(movement.get("item_id"))
        totals[item] = totals.get(item, Decimal("0")) + signed_qty(movement.get("qty", 0))
    return totals


__all__ = ["FakeBalancePort", "FakePostingPort", "movement_totals", "signed_qty"]
