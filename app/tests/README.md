# Backend test harness (`app/tests`, S0-05)

Pytest harness shared by every feature-set suite. `pyproject.toml` (owned by S0-01) is the source of
pytest configuration (`asyncio_mode=auto`, markers `integration|contract|slow`); this document covers
the fixtures and profiles.

## Quick start

```bash
# always runnable, no Docker/database required
python3 -m pytest app/tests/unit/harness app/seed/tests -q

# whole suite once feature sets exist
python3 -m pytest app/tests -q
```

## Fixtures (from `app/tests/conftest.py`)

| Fixture | Purpose |
|---|---|
| `settings` | Test `Settings` (env=test, non-secret placeholders, no `.env`) |
| `clock`, `fixed_now`, `uuids`, `uuid7_factory` | Deterministic time and UUIDv7 helpers |
| `tenant`, `tenant_id`, `system_tenant_id` | Deterministic tenant fixtures |
| `rbac_catalogue` | Full RBAC catalogue copy (`fixtures/permissions.json`) |
| `principal`, `principal_readonly`, `principal_factory`, `admin_scopes` | RBAC principals built from seeded role templates |
| `tenant_context` | Binds `app.tests.utils.context.CURRENT_TENANT` for the test |
| `auth_headers` | Placeholder bearer header until FS-00 auth middleware lands (tests should depend on this fixture, not hand-build tokens) |
| `app_instance`, `sync_client`, `client` | FastAPI app factory and sync/async test clients; `pytest.skip` if `app.api.main` is absent |
| `db_config`, `requires_db`, `db_profile_description` | Resolved DB profile + graceful skip |
| `db_engine`, `db_session`, `db_tables`, `db_reset` | Async SQLAlchemy engine/session; `db_session` uses an outer transaction rolled back after each test |
| `uom_pair_eaches`, `item_factory`, `location_factory`, `status_factory`, `posting_factory` | Convenience object factories |

Util modules (`app/tests/utils/`): `deterministic`, `factories`, `assertions`, `invariants`,
`golden`, `db`, `context`. Fixtures payloads live in `app/tests/fixtures/`.

## Database profiles

Integration tests request `db_session`/`requires_db`; without a database they **skip** with a reason.
Resolution (`app/tests/utils/db.py`):

| `TEST_DB_PROFILE` | Behaviour |
|---|---|
| `auto` (default) | `TEST_DATABASE_URL` → running compose Postgres for this repo (any isolated compose project; port auto-detected) → testcontainers (Docker + `testcontainers` + `asyncpg`) → skip |
| `url` | use `TEST_DATABASE_URL` (must be an asyncpg URL) |
| `testcontainers` | start `postgres:16-alpine` for the session (requires Docker + testcontainers extra) |
| `compose` | use `TEST_COMPOSE_DB_URL`, else auto-detect the running repo compose Postgres (credentials from `POSTGRES_USER/PASSWORD/DB`), else `postgresql+asyncpg://inventory:inventory@localhost:5432/inventory_test` |
| `skip` | never run DB tests |

Set `TEST_DB_REPORT=1` to print the resolved profile once at session start. In Colima/rootless-Docker environments the harness disables testcontainers Ryuk (`TESTCONTAINERS_RYUK_DISABLED`) because the socket bind-mount is unsupported; the session container is stopped explicitly at exit. Async tests are discovered
via `asyncio_mode=auto`; DB tests are marked `@pytest.mark.integration`.

## Writing tests (conventions)

- Unit tests: `app/tests/unit/<context>/…`; invariant suites: `app/tests/invariants/…`; contracts:
  `app/tests/contracts/…`; integration: `app/tests/integration/<context>/…`.
- Use the factories (`make_item`, `make_ledger_posting`, …) and assertions (`assert_quantity_string`,
  `assert_utc_timestamp`, `assert_error_envelope`, `assert_scope_denied`) rather than ad-hoc literals.
- Invariant suites subclass/use `InvariantHarness` and name checks with plan ids (`INV-01`…`INV-33`).
- Golden fixtures: compare with `assert_matches_golden`; during development rewrite with
  `UPDATE_GOLDEN=1` and commit the diff (never enabled in CI).
- Prefer dependency-injected fixed time/UUIDs over wall clock/randomness so failures reproduce.
