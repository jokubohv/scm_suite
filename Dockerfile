# syntax=docker/dockerfile:1.7
#
# Inventory Platform — runtime image (S0-02).
# One image, three entrypoints (D-01): `api` (uvicorn), `worker` (RQ), `outbox-relay`
# (relay entrypoint added by FS-00; see docker-compose.yml). The same image runs migrations
# (`alembic upgrade head`) and one-off management commands through `docker compose run`.
#
# Multi-stage:
#   builder  — resolves/compiles Python dependencies from pyproject.toml into /opt/venv
#   runtime  — production image: venv + app source + alembic.ini, non-root UID/GID 10001
#   dev      — local/dev compose target: adds `.[dev]` tooling + read-only plan/contract sources
#              so `docker compose run --rm api pytest ...` (L3 exit gates) works in-container.
#
# Usage:
#   docker build --target runtime -t inventory-platform-api:runtime .
#   docker build --target dev     -t inventory-platform-api:dev     .   # compose local stack
#
# Security: no secrets are baked into the image; all runtime configuration comes from the
# environment (see .env.example). The image contains no credentials, keys or plan documents
# beyond `design/permissions.json` (the read-only RBAC catalogue used by the seed loader).

ARG PYTHON_VERSION=3.12

# --------------------------------------------------------------------------------------------
# Stage 1 — builder: install runtime dependencies from pyproject.toml into a dedicated venv.
# --------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# build-essential is only needed if a dependency lacks a wheel; it never reaches the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# The project metadata references README.IMPLEMENTATION.md (pyproject readme); copy it before
# installing. Source is copied before `pip install .` because setuptools builds the `app` package.
COPY pyproject.toml README.IMPLEMENTATION.md ./
COPY app ./app

RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --upgrade pip \
    && /opt/venv/bin/pip install .

# --------------------------------------------------------------------------------------------
# Stage 2 — runtime: production shape (python:3.12-slim, non-root, tini as PID 1).
# --------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="inventory-platform" \
      org.opencontainers.image.description="NetSuite-like inventory/SCM backend (Phase 1 backend, D-01 modular monolith)" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.source="inventory-platform"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Runtime deps (and the installed `app` package) from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# Source of truth at /app: the app package, alembic config/chain and the read-only RBAC catalogue
# used by the seed loader (app/seed/constants.py resolves repo design/ when present).
COPY alembic.ini pyproject.toml README.IMPLEMENTATION.md ./
COPY app ./app
COPY design/permissions.json ./design/permissions.json

# Runtime files must be readable by the non-root app user (source files may be mode 600).
RUN chown -R 10001:10001 /app && chmod -R a+rX /app

# Drop privileges: runtime processes never run as root (S0-02 requirement, INV security posture).
USER 10001:10001

EXPOSE 8000

# tini reaps zombies and forwards SIGTERM/SIGINT to the entrypoint process.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default entrypoint = api. Compose overrides `command` for worker/outbox-relay.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

# --------------------------------------------------------------------------------------------
# Stage 3 — dev: compose local-stack target. Adds dev dependencies (pytest/ruff/mypy) and the
# read-only plan/contract sources needed by in-container gates:
#   docker compose run --rm api pytest app/tests/... -q
#   docker compose run --rm api alembic upgrade head
#   docker compose run --rm api python -m app.seed.cli --tenant system
# Never use this target for staging/prod (use `runtime`).
# --------------------------------------------------------------------------------------------
FROM runtime AS dev

USER root

COPY pyproject.toml README.IMPLEMENTATION.md ./
COPY app ./app
RUN /opt/venv/bin/pip install --no-cache-dir -e ".[dev]"

COPY design ./design
COPY feature-sets ./feature-sets
COPY contracts ./contracts
COPY tools ./tools
COPY scripts ./scripts
COPY .env.example ./

# Editable install writes egg-info/pytest caches under /app; keep them writable by the non-root user.
RUN chown -R 10001:10001 /app

USER 10001:10001
