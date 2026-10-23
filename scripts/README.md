# Operational scripts (S0-06)

Fail-fast, idempotent wrappers for the deployment/gate protocol (`PLAN.md` §7.3–§7.5,
`design/PARALLEL-IMPLEMENTATION-ADDENDUM.md` §5–§7). All scripts:

- start with `set -euo pipefail` and print a usage header (`-h`/`--help`);
- are **idempotent** (safe to re-run) and **deterministic**;
- **never read or write secrets** — tokens/URLs/secrets come from the environment or a secret manager and
  are never echoed (client secrets are passed as `*_secret_ref` pointers only);
- **degrade gracefully**: when docker/git/a DB/plan tooling is missing the script says exactly what is
  missing and exits with a distinct dependency code instead of half-applying a change.

Exit-code convention: `0` success · `1` operation failed · `2` usage error · `3` dependency/unavailable.

## Scripts

| Script | Purpose | Key environment |
|---|---|---|
| `check_env.sh FS-XX ENV [--no-probe]` | L2 entry gate: `tools/feature_sets.py check-env` + optional `/readyz` probe | `API_BASE_URL`, `API_TOKEN` (optional) |
| `deploy_set.sh FS-XX ENV [--dry-run] [--run-exit-checks] [--record] …` | L0 validate → entry gate → `alembic upgrade head` → local dev compose refresh → L3 exit checks | `DATABASE_URL`/`ALEMBIC_DATABASE_URL`, `PYTHON` |
| `enable_flag.sh FS-XX ENV [--tenant ID] [--disable] [--dry-run]` | PUT the manifest flag per tenant (default off in code; enabled after L3) | `API_BASE_URL`, `API_TOKEN` |
| `provision_sso_dex.sh --tenant T --protocol SAML\|OIDC\|LDAP_AD …` | Deterministically render one per-connection Dex OIDC broker config (secret-manager refs only) | `DEX_DB_*`, `PLATFORM_SSO_CALLBACK_URL` (at deploy) |
| `seed.sh ENV [--dry-run] [--check]` | Run the idempotent seed/reference loader (`app/seed/`, S0-07) | `APP_ENV`/`INV_ENV` |
| `new_migration.sh REVISION_ID -m "message" [--autogenerate]` | Create an Alembic revision with a pre-allocated 4-digit id, then validate the chain | `DATABASE_URL` (only for `--autogenerate`) |
| `worktree.sh CARD [BASE_REF]` / `--list` / `--remove CARD` | Per-card git worktree + `feat/CARD` branch (Δ-PAR §6) | `WORKTREE_DIR` |
| `up.sh [--profile dev\|test\|sso] [--build] [--wait] [SERVICE…]` | Start/refresh the local compose stack for this namespace | `COMPOSE_PROJECT_NAME` |
| `down.sh [--volumes]` | Stop the local compose stack (idempotent; `--volumes` destroys local data) | `COMPOSE_PROJECT_NAME` |

`_common.sh` is sourced by all scripts (logging, argument validation, compose/alembic discovery) and is
not executable on its own.

## Typical flows

```bash
# local dev
scripts/up.sh --profile dev
scripts/check_env.sh FS-00 dev
scripts/deploy_set.sh FS-00 dev --run-exit-checks
scripts/enable_flag.sh FS-00 dev --tenant dev-tenant

# migrations only (CI/shadow DB)
DATABASE_URL=postgresql+asyncpg://… alembic -c alembic.ini upgrade head
python -m app.migrations.chain_guard

# per-card worktree
scripts/worktree.sh FS-00 integration/B1
```

## Notes

- `deploy_set.sh` only manages the **local dev** stack; staging/prod deployment is CI/CD-owned (the script
  prints the commands instead of touching remote environments).
- `DATA-MODEL` migration numbers are pre-allocated; the chain is validated by
  `python -m app.migrations.chain_guard` (no database required).
- `up.sh`/`down.sh` require docker compose but are never needed by lint/unit tests, so CI passes on a
  machine without Docker.
