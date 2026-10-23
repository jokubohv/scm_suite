"""Δ-CFG compatibility engine integration paths (exit gate): dual scope, HARD/WARN, override,
immutable evidence, configuration validation/simulation and report."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

pytestmark = pytest.mark.integration

TODAY = date.today().isoformat()


async def _bom_with_two(client, world, *, code: str) -> dict[str, Any]:
    headers = world["headers"]
    bom = await client.post(
        "/api/v1/boms",
        json={
            "assembly_item_id": str(world["assembly_item_id"]),
            "bom_code": code,
            "name": code,
        },
        headers=headers,
    )
    await client.post(f"/api/v1/boms/{bom.json()['bom']['id']}/activate", headers=headers)
    revision = await client.post(
        f"/api/v1/boms/{bom.json()['bom']['id']}/revisions",
        json={
            "revision_code": "R1",
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    revision_id = revision.json()["revision"]["id"]
    for line_number, item_id, qty in (
        (1, world["component_item_id"], "2"),
        (2, world["other_item_id"], "1"),
    ):
        response = await client.post(
            f"/api/v1/bom-revisions/{revision_id}/components",
            json={
                "line_number": line_number,
                "component_item_id": str(item_id),
                "qty_per": qty,
                "uom_id": str(world["uom_id"]),
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
    activated = await client.post(f"/api/v1/bom-revisions/{revision_id}/activate", headers=headers)
    assert activated.status_code == 200, activated.text
    return {"bom_id": bom.json()["bom"]["id"], "revision_id": revision_id}


async def _attribute_def(client, world) -> None:
    response = await client.post(
        "/api/v1/manufacturing/compat/attributes",
        json={
            "code": "socket",
            "name": "CPU Socket",
            "data_type": "TEXT",
        },
        headers=world["headers"],
    )
    assert response.status_code == 201, response.text


async def _set_socket(client, world, item_id, value: str) -> None:
    response = await client.put(
        f"/api/v1/items/{item_id}/compat-attributes",
        json={
            "values": [{"attribute_code": "socket", "value": value}],
        },
        headers=world["headers"],
    )
    assert response.status_code == 200, response.text


async def _hard_rule_set(client, world, *, code: str, rule_code: str) -> str:
    headers = world["headers"]
    created = await client.post(
        "/api/v1/manufacturing/compat/rule-sets",
        json={
            "code": code,
            "name": code,
            "scope_type": "ASSEMBLY",
            "scope_ref_id": str(world["assembly_item_id"]),
            "priority": 10,
            "enforcement_mode": "HARD",
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    rule_set_id = created.json()["rule_set"]["id"]
    rule = await client.post(
        f"/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/rules",
        json={
            "code": rule_code,
            "rule_type": "ATTR_EQ",
            "subject_type": "ATTRIBUTE",
            "constraint_expr": {"attribute": "socket", "value": "LGA1700"},
            "severity": "HARD",
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    assert rule.status_code == 201, rule.text
    activated = await client.post(f"/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/activate", headers=headers)
    assert activated.status_code == 200, activated.text
    return rule_set_id


def _wo_payload(world, **extra: Any) -> dict[str, Any]:
    return {
        "assembly_item_id": str(world["assembly_item_id"]),
        "qty_ordered": "5",
        "start_date": TODAY,
        "end_date": TODAY,
        **extra,
    }


async def test_dual_scope_hard_block_override_and_evidence(client, world) -> None:
    await _bom_with_two(client, world, code="COMPAT-BOM")
    await _attribute_def(client, world)
    await _set_socket(client, world, world["component_item_id"], "AM5")
    rule_set_id = await _hard_rule_set(client, world, code="SOCKET-RULE", rule_code="CPU-SOCKET")

    blocked = await client.post("/api/v1/work-orders", json=_wo_payload(world), headers=world["headers"])
    assert blocked.status_code == 409, blocked.text
    body = blocked.json()
    assert body["code"] == "COMPATIBILITY_VIOLATION"

    overridden = await client.post(
        "/api/v1/work-orders",
        json=_wo_payload(
            world,
            compat_override=True,
            compat_override_reason="approved deviation DEV-1",
        ),
        headers=world["headers"],
    )
    assert overridden.status_code == 201, overridden.text
    assert overridden.json()["work_order"]["status"] == "PLANNED"

    configurations = await client.get(
        "/api/v1/manufacturing/configurations",
        params={"assembly_item_id": str(world["assembly_item_id"])},
        headers=world["headers"],
    )
    assert configurations.status_code == 200
    results = {row["result"] for row in configurations.json()["items"]}
    assert "HARD_FAIL" in results and "OVERRIDDEN" in results
    record = next(row for row in configurations.json()["items"] if row["result"] == "OVERRIDDEN")
    assert record["override_reason"] == "approved deviation DEV-1"
    assert record["configuration_hash"]
    assert record["matched_rule_sets"]

    # Immutable evidence: no update endpoint exists; append-only trigger enforces at DB level too.
    report = await client.get("/api/v1/manufacturing/compat/report", headers=world["headers"])
    assert report.status_code == 200
    assert report.json()["with_violations"] >= 1

    retired = await client.post(
        f"/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/retire", headers=world["headers"]
    )
    assert retired.status_code == 200, retired.text


async def test_context_scope_rules_apply_with_and_semantics(client, world) -> None:
    await _bom_with_two(client, world, code="CONTEXT-BOM")
    headers = world["headers"]
    created = await client.post(
        "/api/v1/manufacturing/compat/rule-sets",
        json={
            "code": "CUSTOMER-EXCLUDE",
            "name": "Customer exclude",
            "scope_type": "CUSTOMER",
            "scope_ref_text": "cust-1",
            "priority": 5,
            "enforcement_mode": "HARD",
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    rule_set_id = created.json()["rule_set"]["id"]
    rule = await client.post(
        f"/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/rules",
        json={
            "code": "NO-COMBO",
            "rule_type": "EXCLUDES",
            "subject_type": "ITEM_SET",
            "constraint_expr": {
                "left_items": [str(world["component_item_id"])],
                "right_items": [str(world["other_item_id"])],
            },
            "severity": "HARD",
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    assert rule.status_code == 201, rule.text
    activated = await client.post(f"/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/activate", headers=headers)
    assert activated.status_code == 200, activated.text

    plain = await client.post("/api/v1/work-orders", json=_wo_payload(world), headers=headers)
    assert plain.status_code == 201, plain.text
    contextual = await client.post(
        "/api/v1/work-orders",
        json=_wo_payload(
            world,
            compat_context_refs={"customer_ref": "cust-1"},
        ),
        headers=headers,
    )
    assert contextual.status_code == 409, contextual.text
    assert contextual.json()["code"] == "COMPATIBILITY_VIOLATION"


async def test_validate_simulate_and_representation(client, world) -> None:
    await _bom_with_two(client, world, code="VALIDATE-BOM")
    headers = world["headers"]
    validation = await client.post(
        "/api/v1/manufacturing/configurations/validate",
        json={
            "assembly_item_id": str(world["assembly_item_id"]),
            "context": "ORDER",
        },
        headers=headers,
    )
    assert validation.status_code == 200, validation.text
    assert validation.json()["result"] in {"VALID", "WARN"}

    simulation = await client.post(
        "/api/v1/manufacturing/configurations/simulate",
        json={
            "assembly_item_id": str(world["assembly_item_id"]),
            "context": "ORDER",
            "components": [{"item_id": str(world["component_item_id"]), "qty": "1"}],
        },
        headers=headers,
    )
    assert simulation.status_code == 200, simulation.text
    assert simulation.json()["simulate"] is True
