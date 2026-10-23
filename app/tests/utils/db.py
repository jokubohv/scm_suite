"""Database profile resolution for tests (URL / testcontainers / compose / graceful skip).

Design goals:
* **Never fail the harness just because Docker/Postgres is absent** — integration fixtures skip with a
  reason instead (S0-05 exit check: "test harness green on empty stack").
* Support three real profiles, in precedence order for ``TEST_DB_PROFILE=auto``:
  ``url`` (``TEST_DATABASE_URL``) → ``testcontainers`` (Docker + ``testcontainers`` + ``asyncpg``)
  → ``compose`` (repo compose stack Postgres) → ``skip``.
* Keep resolution pure/testable: availability checks can be injected as booleans.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE_URL = "url"
PROFILE_TESTCONTAINERS = "testcontainers"
PROFILE_COMPOSE = "compose"
PROFILE_SKIP = "skip"
DB_PROFILES = (PROFILE_URL, PROFILE_TESTCONTAINERS, PROFILE_COMPOSE, PROFILE_SKIP)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COMPOSE_DB_URL = "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory_test"
DEFAULT_COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

_shared_container: Any | None = None


class DatabaseUnavailable(RuntimeError):
    """Raised when an integration test needs a database that this environment cannot provide."""


@dataclass(frozen=True)
class DbProfileConfig:
    profile: str
    url: str | None
    reason: str = ""
    asyncpg_available: bool = False
    docker_available: bool = False
    testcontainers_available: bool = False

    @property
    def available(self) -> bool:
        return self.profile != PROFILE_SKIP

    @property
    def is_integration(self) -> bool:
        return self.available


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def docker_available(timeout_seconds: int = 5) -> bool:
    """Best-effort Docker daemon check (false on any failure; never raises)."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def testcontainers_available() -> bool:
    if not _module_available("testcontainers"):
        return False
    return _module_available("testcontainers.community.postgres") or _module_available("testcontainers.postgres")


def asyncpg_available() -> bool:
    return _module_available("asyncpg")


def compose_postgres_available(
    compose_file: Path = DEFAULT_COMPOSE_FILE, timeout_seconds: int = 5
) -> bool:
    """True when a repo compose Postgres container is running (compose file may not exist yet)."""
    return detect_compose_postgres_url(compose_file, timeout_seconds=timeout_seconds) is not None


def detect_compose_postgres_url(
    compose_file: Path = DEFAULT_COMPOSE_FILE, *, timeout_seconds: int = 5
) -> str | None:
    """Best-effort URL of the running repo compose Postgres, including isolated compose projects.

    1. ``TEST_COMPOSE_DB_URL`` always wins when set.
    2. The default compose project (``docker compose ps -q postgres``) is probed for its published port.
    3. Any compose project whose working dir is this repo is found via container labels (used by
       ``scripts/up.sh`` isolated projects such as ``inv-s0-02-test``).
    Credentials come from ``POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB`` env (compose defaults when unset).
    Returns ``None`` when no running container can be resolved; never raises.
    """
    explicit = os.environ.get("TEST_COMPOSE_DB_URL")
    if explicit:
        return explicit
    if shutil.which("docker") is None:
        return None
    user = os.environ.get("POSTGRES_USER", "inventory")
    password = os.environ.get("POSTGRES_PASSWORD", "inventory-dev-password")
    database = os.environ.get("POSTGRES_DB", "inventory")

    def _url(host_port: str) -> str:
        return f"postgresql+asyncpg://{user}:{password}@localhost:{host_port}/{database}"

    if compose_file.is_file():
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "ps", "-q", "postgres"],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            container_id = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
            if result.returncode == 0 and container_id:
                port = _container_host_port(container_id, "5432", timeout_seconds)
                if port:
                    return _url(port)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project.working_dir={REPO_ROOT}",
                "--filter",
                "label=com.docker.compose.service=postgres",
                "--format",
                "{{.ID}}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        container_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return None
    for container_id in container_ids:
        port = _container_host_port(container_id, "5432", timeout_seconds)
        if port:
            return _url(port)
    return None


def _container_host_port(container_id: str, container_port: str, timeout_seconds: int) -> str | None:
    """Extract the published host port for a container port (``docker port``); None if unmapped."""
    try:
        result = subprocess.run(
            ["docker", "port", container_id, f"{container_port}/tcp"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        text = line.strip()
        if "->" in text:  # `docker ps` style "0.0.0.0:5432->5432/tcp"
            text = text.split("->", 1)[0]
        host = text.rsplit(":", 1)[-1].strip()
        if host.isdigit():
            return host
    return None


def resolve_db_profile(
    env: Mapping[str, str] | None = None,
    *,
    docker: bool | None = None,
    testcontainers: bool | None = None,
    compose_stack: bool | None = None,
    asyncpg: bool | None = None,
) -> DbProfileConfig:
    """Resolve the effective DB profile; returns ``skip`` with a human-readable reason when absent."""
    env = os.environ if env is None else env
    requested = str(env.get("TEST_DB_PROFILE", "auto")).strip().lower() or "auto"
    docker_ok = docker_available() if docker is None else docker
    container_ok = testcontainers_available() if testcontainers is None else testcontainers
    asyncpg_ok = asyncpg_available() if asyncpg is None else asyncpg
    url_env = env.get("TEST_DATABASE_URL")
    compose_ok = compose_postgres_available() if compose_stack is None else compose_stack

    if requested not in ("auto", *DB_PROFILES):
        raise ValueError(f"invalid TEST_DB_PROFILE {requested!r}; expected auto|url|testcontainers|compose|skip")

    if requested == PROFILE_SKIP:
        return DbProfileConfig(
            profile=PROFILE_SKIP,
            url=None,
            reason="TEST_DB_PROFILE=skip",
            asyncpg_available=asyncpg_ok,
            docker_available=docker_ok,
            testcontainers_available=container_ok,
        )

    if requested == PROFILE_URL:
        if not url_env:
            return _skip("TEST_DB_PROFILE=url requires TEST_DATABASE_URL", asyncpg_ok, docker_ok, container_ok)
        if not asyncpg_ok:
            return _skip("asyncpg is not installed (pip install -e .[dev] or pip install asyncpg)", asyncpg_ok, docker_ok, container_ok)
        return DbProfileConfig(PROFILE_URL, url_env, "", asyncpg_ok, docker_ok, container_ok)

    if requested == PROFILE_TESTCONTAINERS:
        reason = _testcontainers_blocker(docker_ok, container_ok, asyncpg_ok)
        if reason:
            return _skip(reason, asyncpg_ok, docker_ok, container_ok)
        return DbProfileConfig(PROFILE_TESTCONTAINERS, None, "", asyncpg_ok, docker_ok, container_ok)

    if requested == PROFILE_COMPOSE:
        if not compose_ok:
            return _skip("compose profile requested but no running compose Postgres container", asyncpg_ok, docker_ok, container_ok)
        if not asyncpg_ok:
            return _skip("asyncpg is not installed", asyncpg_ok, docker_ok, container_ok)
        return DbProfileConfig(
            PROFILE_COMPOSE,
            env.get("TEST_COMPOSE_DB_URL") or detect_compose_postgres_url() or DEFAULT_COMPOSE_DB_URL,
            "",
            asyncpg_ok,
            docker_ok,
            container_ok,
        )

    # auto: prefer an explicitly configured URL, then the already-running compose stack (fast, no
    # image pull), then a session testcontainer, then skip gracefully.
    if url_env:
        if not asyncpg_ok:
            return _skip("TEST_DATABASE_URL is set but asyncpg is not installed", asyncpg_ok, docker_ok, container_ok)
        return DbProfileConfig(PROFILE_URL, url_env, "", asyncpg_ok, docker_ok, container_ok)
    if compose_ok and asyncpg_ok:
        return DbProfileConfig(
            PROFILE_COMPOSE,
            env.get("TEST_COMPOSE_DB_URL") or detect_compose_postgres_url() or DEFAULT_COMPOSE_DB_URL,
            "",
            asyncpg_ok,
            docker_ok,
            container_ok,
        )
    reason = _testcontainers_blocker(docker_ok, container_ok, asyncpg_ok)
    if reason is None:
        return DbProfileConfig(PROFILE_TESTCONTAINERS, None, "", asyncpg_ok, docker_ok, container_ok)
    return _skip(
        "no test database available: " + reason + " (set TEST_DATABASE_URL, start the compose stack, "
        "or install testcontainers+asyncpg)",
        asyncpg_ok,
        docker_ok,
        container_ok,
    )


def _testcontainers_blocker(docker_ok: bool, container_ok: bool, asyncpg_ok: bool) -> str | None:
    if not docker_ok:
        return "Docker daemon is not available"
    if not container_ok:
        return "testcontainers[postgres] is not installed"
    if not asyncpg_ok:
        return "asyncpg is not installed"
    return None


def _skip(reason: str, asyncpg_ok: bool, docker_ok: bool, container_ok: bool) -> DbProfileConfig:
    return DbProfileConfig(
        profile=PROFILE_SKIP,
        url=None,
        reason=reason,
        asyncpg_available=asyncpg_ok,
        docker_available=docker_ok,
        testcontainers_available=container_ok,
    )


def to_async_url(url: str) -> str:
    """Normalise a Postgres URL to the asyncpg driver (testcontainers defaults to psycopg2)."""
    for prefix in ("postgresql+psycopg2://", "postgresql+psycopg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


def start_postgres_container() -> str:
    """Start a session-scoped Postgres testcontainer and return an asyncpg URL.

    Safe to call repeatedly; the container is stopped at interpreter exit. A configurable timeout
    (``TEST_DB_START_TIMEOUT_SECONDS``, default 90 s) prevents a slow/offline image pull from hanging
    the whole test session; on timeout the caller should skip.
    """
    import threading

    global _shared_container
    if _shared_container is not None:
        return to_async_url(_shared_container.get_connection_url())
    if not testcontainers_available():
        raise DatabaseUnavailable("testcontainers[postgres] is not installed")
    if not docker_available():
        raise DatabaseUnavailable("Docker daemon is not available")
    try:  # new namespace first (deprecation-free); fall back to the legacy path
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - older testcontainers releases
        from testcontainers.postgres import PostgresContainer

    # Ryuk needs to bind-mount the Docker socket, which some Docker Desktop/Colima setups reject
    # (socket mount path). The session container is stopped explicitly at interpreter exit instead.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    container = PostgresContainer("postgres:16-alpine")
    timeout = float(os.environ.get("TEST_DB_START_TIMEOUT_SECONDS", "90"))
    outcome: dict[str, Any] = {}

    def _start() -> None:
        try:
            container.start()
            outcome["ok"] = True
        except Exception as exc:  # noqa: BLE001 - environment boundary
            outcome["error"] = exc

    thread = threading.Thread(target=_start, name="testcontainers-start", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise DatabaseUnavailable(
            f"Postgres testcontainer did not start within {timeout:.0f}s "
            "(Docker image pull may be unavailable; set TEST_DATABASE_URL or start the compose stack)"
        )
    if "error" in outcome:
        raise DatabaseUnavailable(f"could not start Postgres testcontainer: {outcome['error']}") from outcome["error"]
    _shared_container = container
    atexit.register(_stop_postgres_container)
    return to_async_url(container.get_connection_url())


def _stop_postgres_container() -> None:  # pragma: no cover - interpreter shutdown
    global _shared_container
    if _shared_container is not None:
        try:
            _shared_container.stop()
        except Exception:  # noqa: BLE001 - best effort
            pass
        _shared_container = None


async def check_connection(url: str) -> None:
    """Open one connection and run ``SELECT 1``; raises :class:`DatabaseUnavailable` on failure."""
    if not asyncpg_available():
        raise DatabaseUnavailable("asyncpg is not installed")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - environment boundary
        raise DatabaseUnavailable(f"database not reachable at {url.split('@')[-1]}: {exc}") from exc
    finally:
        await engine.dispose()


def ensure_database_url(config: DbProfileConfig) -> str:
    """Return a usable async URL or raise :class:`DatabaseUnavailable` with the skip reason."""
    if config.profile == PROFILE_SKIP:
        raise DatabaseUnavailable(config.reason or "database profile is skip")
    if config.profile == PROFILE_URL:
        if not config.url:
            raise DatabaseUnavailable("url profile without TEST_DATABASE_URL")
        return config.url
    if config.profile == PROFILE_COMPOSE:
        return config.url or DEFAULT_COMPOSE_DB_URL
    if config.profile == PROFILE_TESTCONTAINERS:
        return start_postgres_container()
    raise DatabaseUnavailable(f"unknown database profile {config.profile!r}")


def describe(config: DbProfileConfig) -> dict[str, Any]:
    """JSON-safe description used in test diagnostics/README examples."""
    return {
        "profile": config.profile,
        "available": config.available,
        "asyncpg": config.asyncpg_available,
        "docker": config.docker_available,
        "testcontainers": config.testcontainers_available,
        "reason": config.reason,
    }
