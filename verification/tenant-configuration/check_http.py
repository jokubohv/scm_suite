"""Disposable localhost HTTP acceptance; requires migrated operator/app database URLs.

TEST_DATABASE_URL: operator URL for synthetic fixtures (localhost only).
DATABASE_URL: non-superuser login inheriting production platform_app/platform_auth grants.
Never point this script at a shared database: append-only fixtures remain until DB disposal.
"""

import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.models import platform as p
from app.core.repositories.api_keys import derive_api_key_pepper
from app.core.security.tokens import ApiKeyHasher

PATH = "/api/v1/admin/tenant-configuration"
ADMIN = os.environ["TEST_DATABASE_URL"]
assert os.environ["ENV"] == "test"
assert make_url(ADMIN).host in {"localhost", "127.0.0.1"}
assert make_url(os.environ["DATABASE_URL"]).host in {"localhost", "127.0.0.1"}
TENANTS = [uuid4() for _ in range(3)]


async def seed():
    engine = create_async_engine(ADMIN)
    keys = []
    hasher = ApiKeyHasher(derive_api_key_pepper(get_settings().jwt_secret))
    async with engine.begin() as db:
        await db.execute(
            text("INSERT INTO platform.currency (code, numeric_code) VALUES ('BOB', 68) ON CONFLICT DO NOTHING")
        )
        for tenant_id in TENANTS:
            await db.execute(
                insert(p.tenant).values(
                    id=tenant_id,
                    tenant_id=tenant_id,
                    code=f"T{tenant_id.hex[:12]}",
                    name="Synthetic configuration HTTP fixture",
                    base_currency_code="USD",
                )
            )
            material = hasher.generate()
            await db.execute(
                insert(p.api_key).values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    name="synthetic-admin",
                    key_prefix=material.key_prefix,
                    secret_hash=material.secret_hash,
                    scopes=["platform.admin"],
                )
            )
            keys.append({"X-API-Key": material.full_key})
    await engine.dispose()
    return keys


async def acceptance(base_url, keys):
    engine = create_async_engine(ADMIN)
    results = {}
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:

            async def patch(index, code, version):
                return await client.patch(
                    PATH, headers={**keys[index], "If-Match": version}, json={"base_currency_code": code}
                )

            before = await client.get(PATH, headers=keys[0])
            assert before.status_code == 200, before.text
            changed = await patch(0, "BOB", before.headers["etag"])
            assert changed.status_code == 200, changed.text
            assert changed.json()["base_currency_code"] == "BOB"
            persisted = await client.get(PATH, headers=keys[0])
            assert persisted.json() == changed.json(), persisted.text
            assert (await client.get(PATH, headers=keys[1])).json()["base_currency_code"] == "USD"
            results["durable_bob_change"] = changed.status_code
            results["other_tenant_unchanged"] = True

            # A real, uncommitted append holds the FK lock on this tenant. The endpoint must
            # reject the race, then reject the committed history (even though its total is zero).
            async with engine.connect() as writer:
                tx = await writer.begin()
                await writer.execute(
                    text("""
                    INSERT INTO inventory_ledger.inventory_transaction
                      (id, tenant_id, transaction_type, transaction_date, ledger_sequence, currency_code)
                    VALUES (:id, :tenant_id, 'T-05', CURRENT_DATE, 1, 'BOB')
                """),
                    {"id": uuid4(), "tenant_id": TENANTS[0]},
                )
                busy = await patch(0, "EUR", changed.headers["etag"])
                assert busy.status_code == 409 and busy.json()["code"] == "CONFIGURATION_BUSY", busy.text
                await tx.commit()
            blocked = await patch(0, "EUR", changed.headers["etag"])
            assert blocked.status_code == 409 and blocked.json()["code"] == "BASE_CURRENCY_IN_USE", blocked.text
            results["concurrent_append"] = busy.json()["code"]
            results["committed_zero_history"] = blocked.json()["code"]

            # An update may introduce a cost into an existing zero-valued row without checking
            # the tenant FK. Mutable tables are locked too; retry is allowed after rollback.
            center = uuid4()
            async with engine.begin() as writer:
                await writer.execute(
                    text("""
                    INSERT INTO manufacturing.work_center (id, tenant_id, code, name, cost_per_hour)
                    VALUES (:id, :tenant_id, 'ZERO', 'Synthetic zero cost', 0)
                """),
                    {"id": center, "tenant_id": TENANTS[1]},
                )
            async with engine.connect() as writer:
                tx = await writer.begin()
                await writer.execute(
                    text("UPDATE manufacturing.work_center SET cost_per_hour = 25 WHERE id = :id"), {"id": center}
                )
                busy = await patch(1, "BOB", '"1"')
                assert busy.status_code == 409 and busy.json()["code"] == "CONFIGURATION_BUSY", busy.text
                await tx.rollback()
            resumed = await patch(1, "BOB", '"1"')
            assert resumed.status_code == 200, resumed.text
            results["concurrent_cost_update"] = busy.json()["code"]
            results["retry_after_rollback"] = resumed.status_code

            races = await asyncio.gather(patch(2, "BOB", '"1"'), patch(2, "EUR", '"1"'))
            assert sum(r.status_code == 200 for r in races) == 1, [r.text for r in races]
            loser = next(r for r in races if r.status_code != 200)
            assert loser.status_code in {409, 412}, loser.text
            winner = next(r for r in races if r.status_code == 200)
            assert (await client.get(PATH, headers=keys[2])).json() == winner.json()
            results["concurrent_configuration_statuses"] = sorted(r.status_code for r in races)

        async with engine.connect() as db:
            audit_count = await db.scalar(
                text(
                    "SELECT count(*) FROM platform.audit_event "
                    "WHERE action = 'tenant.configuration.updated' AND tenant_id = ANY(:tenants)"
                ),
                {"tenants": TENANTS},
            )
            assert audit_count == 3, audit_count
            assert await db.scalar(select(p.tenant.c.base_currency_code).where(p.tenant.c.id == TENANTS[0])) == "BOB"
        results["committed_audits"] = audit_count
        app_engine = create_async_engine(os.environ["DATABASE_URL"])
        async with app_engine.connect() as db:
            role = (
                await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
            ).one()
            assert role == (False, False), role
        await app_engine.dispose()
        results["app_role_superuser_or_bypassrls"] = False
        return results
    finally:
        await engine.dispose()


if __name__ == "__main__":
    keys = asyncio.run(seed())
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    log_path = Path("/tmp/scm-tenant-config-http-server.log")
    with log_path.open("w") as log:
        server = subprocess.Popen(
            ["python3", "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1", "--port", str(port)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=base_url, timeout=10) as client:
                for _ in range(80):
                    try:
                        if client.get("/healthz").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.25)
                else:
                    raise AssertionError(f"Server did not start; inspect {log_path}")
            result = {
                "boundary": "localhost HTTP, real API keys, PostgreSQL 16 production grants",
                **asyncio.run(acceptance(base_url, keys)),
            }
            output = Path(__file__).with_name("http-result.json")
            output.write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
        finally:
            server.terminate()
            server.wait(timeout=15)
