"""Sequence allocator tests: pure policy branches + live-DB allocation/concurrency."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models.platform import number_sequence
from app.core.sequences import (
    DOC_TYPE_PATTERN,
    GapPolicy,
    InvalidSequenceDefinition,
    InvalidSequenceRequest,
    ResetPolicy,
    SequenceDefinition,
    SequenceService,
    UnknownSequenceType,
    compute_period_key,
    declared_sequences,
    format_sequence_value,
)
from app.tests.unit.core_services.sequences.support import (
    cleanup_committed,
    create_tenant,
    create_tenant_committed,
)

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- pure tests --------
def test_declared_catalogue_matches_seed() -> None:
    declarations = declared_sequences()
    assert len(declarations) == 27
    assert declarations["PO"].prefix == "PO"
    assert declarations["PO"].padding == 6
    assert declarations["IMPORT"].prefix == "IMP"
    assert all(definition.reset_policy is ResetPolicy.NEVER for definition in declarations.values())
    assert all(definition.gap_policy is GapPolicy.NON_GAPLESS for definition in declarations.values())


def test_period_keys_and_formatting() -> None:
    january = datetime(2026, 1, 15, tzinfo=UTC)
    february = datetime(2026, 2, 1, tzinfo=UTC)
    assert compute_period_key(ResetPolicy.NEVER, january) is None
    assert compute_period_key(ResetPolicy.ANNUAL, january) == "2026"
    assert compute_period_key(ResetPolicy.MONTHLY, january) == "2026-01"
    assert compute_period_key(ResetPolicy.MONTHLY, february) == "2026-02"
    with pytest.raises(InvalidSequenceRequest):
        compute_period_key(ResetPolicy.MONTHLY, datetime(2026, 1, 1))  # noqa: DTZ001 - naive rejected
    assert format_sequence_value("PO", 6, 1) == "PO000001"
    assert format_sequence_value("X", 3, 42) == "X042"
    with pytest.raises(InvalidSequenceDefinition):
        format_sequence_value("X", 0, 1)
    with pytest.raises(InvalidSequenceRequest):
        format_sequence_value("X", 3, 0)


def test_definition_validation() -> None:
    assert DOC_TYPE_PATTERN.match("PO")
    with pytest.raises(InvalidSequenceDefinition):
        SequenceDefinition(doc_type="bad type")
    with pytest.raises(InvalidSequenceDefinition):
        SequenceDefinition(doc_type="PO", padding=99)
    with pytest.raises(InvalidSequenceRequest):
        SequenceService.__new__(SequenceService).definition("nope!", allow_undeclared=False)


# --------------------------------------------------------------------------- DB tests ----------
async def test_sequential_allocation_per_tenant_and_doc_type(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="SEQ-A")
    other_tenant = await create_tenant(db_session, code="SEQ-B")
    service = SequenceService(db_session)

    first = await service.next(tenant_id, "PO")
    assert (first.value, first.formatted, first.period_key, first.reset_policy) == (
        1,
        "PO000001",
        None,
        ResetPolicy.NEVER,
    )
    second = await service.next(tenant_id, "PO")
    assert second.value == 2 and second.formatted == "PO000002"
    invoice = await service.next(tenant_id, "IR")
    assert invoice.value == 1 and invoice.formatted == "IR000001"
    other = await service.next(other_tenant, "PO")
    assert other.value == 1

    preview = await service.preview(tenant_id, "PO")
    assert preview.value == 3
    assert (await service.next(tenant_id, "PO")).value == 3
    state = await service.state(tenant_id, "PO")
    assert state is not None and state.next_value == 4 and state.last_value == 3

    rows = (
        await db_session.execute(
            select(number_sequence.c.doc_type, number_sequence.c.tenant_id, number_sequence.c.next_value).where(
                number_sequence.c.tenant_id.in_([tenant_id, other_tenant])
            )
        )
    ).all()
    assert len(rows) == 3


async def test_preview_does_not_create_rows(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="SEQ-PREVIEW")
    service = SequenceService(db_session)
    preview = await service.preview(tenant_id, "PO")
    assert preview.value == 1 and preview.formatted == "PO000001"
    count = (
        await db_session.execute(
            select(func.count()).select_from(number_sequence).where(number_sequence.c.tenant_id == tenant_id)
        )
    ).scalar_one()
    assert count == 0
    state = await service.state(tenant_id, "PO")
    assert state is None


async def test_monthly_and_annual_reset_with_injected_clock(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="SEQ-RESET")
    declarations = {
        "PO": SequenceDefinition(doc_type="PO", prefix="PO", reset_policy=ResetPolicy.MONTHLY),
        "IR": SequenceDefinition(doc_type="IR", prefix="IR", reset_policy=ResetPolicy.ANNUAL),
    }
    service = SequenceService(db_session, declarations=declarations)
    january = datetime(2026, 1, 10, tzinfo=UTC)
    february = datetime(2026, 2, 1, tzinfo=UTC)
    next_year = datetime(2027, 1, 1, tzinfo=UTC)

    assert (await service.next(tenant_id, "PO", now=january)).value == 1
    assert (await service.next(tenant_id, "PO", now=january)).value == 2
    february_first = await service.next(tenant_id, "PO", now=february)
    assert (february_first.value, february_first.period_key) == (1, "2026-02")
    assert (await service.next(tenant_id, "PO", now=february)).value == 2

    assert (await service.next(tenant_id, "IR", now=january)).value == 1
    assert (await service.next(tenant_id, "IR", now=january)).value == 2
    annual_reset = await service.next(tenant_id, "IR", now=next_year)
    assert (annual_reset.value, annual_reset.period_key) == (1, "2027")


async def test_unknown_and_explicit_period_validation(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="SEQ-VALID")
    service = SequenceService(db_session)
    with pytest.raises(UnknownSequenceType):
        await service.next(tenant_id, "NOPE")
    undeclared = await service.next(tenant_id, "CUSTOM", allow_undeclared=True)
    assert undeclared.value == 1 and undeclared.formatted == "CUSTOM000001"
    explicit = await service.next(tenant_id, "PO", period_key="2026-Q1")
    assert explicit.period_key == "2026-Q1" and explicit.value == 1
    with pytest.raises(InvalidSequenceRequest):
        await service.next(tenant_id, "PO", period_key="")


async def test_concurrent_allocations_are_unique_and_contiguous(db_engine) -> None:
    tenant_id = await create_tenant_committed(db_engine, code="SEQ-CONC")
    try:

        async def allocate() -> int:
            async with AsyncSession(db_engine) as session:
                value = (await SequenceService(session).next(tenant_id, "PO")).value
                await session.commit()
                return value

        values = sorted(await asyncio.gather(allocate(), allocate(), allocate(), allocate()))
        assert values == [1, 2, 3, 4]
        async with AsyncSession(db_engine) as session:
            state = await SequenceService(session).state(tenant_id, "PO")
        assert state is not None and state.next_value == 5
    finally:
        await cleanup_committed(db_engine, tenant_id)


async def test_tenant_isolation_and_sequence_state(db_session) -> None:
    first = await create_tenant(db_session, code="SEQ-ISO-A")
    second = await create_tenant(db_session, code="SEQ-ISO-B")
    service = SequenceService(db_session)
    for expected in (1, 2):
        assert (await service.next(first, "WO")).value == expected
    assert (await service.next(second, "WO")).value == 1
    first_state = await service.state(first, "WO")
    assert first_state is not None and first_state.next_value == 3
    assert await service.state(second, "WO") is not None
    assert await service.state(uuid.uuid4(), "WO") is None
