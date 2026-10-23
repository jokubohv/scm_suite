"""Custom-field definition admin API (live DB): CRUD, validation, immutability, usage guard."""

from __future__ import annotations

from datetime import UTC
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.models import platform as p
from app.tests.integration.platform_config.conftest import OTHER_TENANT_ID, TENANT_ID

PREFIX = "/api/v1/admin/custom-fields"


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "entity_type": "ITEM",
        "field_code": "warranty_months",
        "label": "Warranty months",
        "data_type": "NUMBER",
        "is_required": False,
        "is_searchable": True,
        "validation": {"min": 0, "max": 120},
    }
    payload.update(overrides)
    return payload


async def test_custom_field_crud(app_client: Any) -> None:
    assert (await app_client.get(PREFIX)).json()["items"] == []

    created = await app_client.post(PREFIX, json=_payload())
    assert created.status_code == 201, created.text
    body = created.json()
    field_id = body["id"]
    assert body["entity_type"] == "ITEM"
    assert body["data_type"] == "NUMBER"
    assert body["validation"] == {"min": 0, "max": 120}
    assert body["version"] == 1

    listed = await app_client.get(PREFIX, params={"entity_type": "item"})
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    fetched = await app_client.get(f"{PREFIX}/{field_id}")
    assert fetched.status_code == 200
    assert fetched.json()["field_code"] == "warranty_months"

    patched = await app_client.patch(
        f"{PREFIX}/{field_id}",
        json={"label": "Warranty (months)", "is_required": True},
        headers={"If-Match": "1"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["label"] == "Warranty (months)"
    assert patched.json()["is_required"] is True
    assert patched.json()["data_type"] == "NUMBER"

    deleted = await app_client.delete(f"{PREFIX}/{field_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted.json()["field_def"]["field_code"] == "warranty_months"
    assert (await app_client.get(f"{PREFIX}/{field_id}")).status_code == 404


async def test_select_options_and_validation_rules(app_client: Any) -> None:
    select_def = await app_client.post(
        PREFIX, json=_payload(field_code="coating", data_type="SELECT", options=["painted", "raw"], validation=None)
    )
    assert select_def.status_code == 201
    assert select_def.json()["options"] == ["painted", "raw"]

    missing_options = await app_client.post(
        PREFIX, json=_payload(field_code="surface", data_type="SELECT", validation=None)
    )
    assert missing_options.status_code == 422
    assert missing_options.json()["code"] == "CUSTOM_FIELD_OPTIONS_REQUIRED"

    text_with_options = await app_client.post(PREFIX, json=_payload(field_code="note", data_type="TEXT", options=["x"]))
    assert text_with_options.status_code == 422
    assert text_with_options.json()["code"] == "CUSTOM_FIELD_OPTIONS_INVALID"

    bad_validation = await app_client.post(PREFIX, json=_payload(field_code="bad_rule", validation={"min": 5, "max": 1}))
    assert bad_validation.status_code == 422
    assert bad_validation.json()["code"] == "CUSTOM_FIELD_VALIDATION_INVALID"

    duplicate = await app_client.post(PREFIX, json=_payload(field_code="warranty_months", label="Dup"))
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "CUSTOM_FIELD_DUPLICATE"


async def test_data_type_is_immutable(app_client: Any) -> None:
    created = await app_client.post(PREFIX, json=_payload(field_code="immutable_type"))
    assert created.status_code == 201
    field_id = created.json()["id"]
    rejected = await app_client.patch(f"{PREFIX}/{field_id}", json={"data_type": "TEXT"})
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "CUSTOM_FIELD_TYPE_IMMUTABLE"


async def test_if_match_stale_version(app_client: Any) -> None:
    created = await app_client.post(PREFIX, json=_payload(field_code="stale_version"))
    assert created.status_code == 201
    stale = await app_client.patch(
        f"{PREFIX}/{created.json()['id']}", json={"label": "New"}, headers={"If-Match": "42"}
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "PRECONDITION_FAILED"


async def test_delete_guarded_when_values_exist(app_client: Any, db_session: Any) -> None:
    created = await app_client.post(PREFIX, json=_payload(field_code="with_values"))
    assert created.status_code == 201
    field_id = UUID(created.json()["id"])
    await db_session.execute(
        p.custom_field_value.insert().values(
            id=uuid4(),
            tenant_id=TENANT_ID,
            field_def_id=field_id,
            entity_id=uuid4(),
            value_number=12,
        )
    )
    await db_session.flush()
    blocked = await app_client.delete(f"{PREFIX}/{field_id}")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "CUSTOM_FIELD_IN_USE"
    # The definition is still readable after the rejected delete.
    assert (await app_client.get(f"{PREFIX}/{field_id}")).status_code == 200


async def test_custom_fields_are_tenant_scoped(app_client: Any, db_session: Any) -> None:
    foreign_id = uuid4()
    await db_session.execute(
        p.custom_field_def.insert().values(
            id=foreign_id,
            tenant_id=OTHER_TENANT_ID,
            entity_type="ITEM",
            field_code="foreign_field",
            label="Foreign",
            data_type="TEXT",
        )
    )
    await db_session.flush()
    assert (await app_client.get(PREFIX)).json()["items"] == []
    assert (await app_client.get(f"{PREFIX}/{foreign_id}")).status_code == 404
    blocked = await app_client.delete(f"{PREFIX}/{foreign_id}")
    assert blocked.status_code == 404


async def test_custom_field_validation_errors(app_client: Any) -> None:
    bad_entity = await app_client.post(PREFIX, json=_payload(entity_type="SPACESHIP"))
    assert bad_entity.status_code == 422
    assert bad_entity.json()["code"] == "CUSTOM_FIELD_ENTITY_TYPE_INVALID"

    bad_code = await app_client.post(PREFIX, json=_payload(field_code="Bad Code"))
    assert bad_code.status_code == 422
    assert bad_code.json()["code"] == "CUSTOM_FIELD_CODE_INVALID"

    bad_type = await app_client.post(PREFIX, json=_payload(data_type="MONEY"))
    assert bad_type.status_code == 422  # pydantic Literal

    bad_entity_filter = await app_client.get(PREFIX, params={"entity_type": "SPACESHIP"})
    assert bad_entity_filter.status_code == 422
    assert bad_entity_filter.json()["code"] == "CUSTOM_FIELD_ENTITY_TYPE_INVALID"
