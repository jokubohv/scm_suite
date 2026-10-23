"""DB profile resolution is pure, precedence-correct and never raises when unavailable."""

from __future__ import annotations

import pytest

from app.tests.utils.db import (
    DB_PROFILES,
    DEFAULT_COMPOSE_DB_URL,
    PROFILE_COMPOSE,
    PROFILE_SKIP,
    PROFILE_TESTCONTAINERS,
    PROFILE_URL,
    DatabaseUnavailable,
    DbProfileConfig,
    describe,
    ensure_database_url,
    resolve_db_profile,
    to_async_url,
)


def test_skip_profile() -> None:
    config = resolve_db_profile({"TEST_DB_PROFILE": "skip"})
    assert config.profile == PROFILE_SKIP
    assert not config.available
    assert "skip" in config.reason
    with pytest.raises(DatabaseUnavailable):
        ensure_database_url(config)


def test_url_profile_requires_url_and_driver() -> None:
    config = resolve_db_profile({"TEST_DB_PROFILE": "url", "TEST_DATABASE_URL": "postgresql+asyncpg://x/y"}, asyncpg=True)
    assert config.profile == PROFILE_URL
    assert config.url == "postgresql+asyncpg://x/y"
    assert ensure_database_url(config) == config.url

    missing = resolve_db_profile({"TEST_DB_PROFILE": "url"}, asyncpg=True)
    assert missing.profile == PROFILE_SKIP and "TEST_DATABASE_URL" in missing.reason

    no_driver = resolve_db_profile(
        {"TEST_DB_PROFILE": "url", "TEST_DATABASE_URL": "postgresql+asyncpg://x/y"}, asyncpg=False
    )
    assert no_driver.profile == PROFILE_SKIP and "asyncpg" in no_driver.reason


def test_testcontainers_profile_requires_docker_testcontainers_asyncpg() -> None:
    ok = resolve_db_profile(
        {"TEST_DB_PROFILE": "testcontainers"}, docker=True, testcontainers=True, asyncpg=True
    )
    assert ok.profile == PROFILE_TESTCONTAINERS
    assert ok.available

    for kwargs, expected in (
        ({"docker": False, "testcontainers": True, "asyncpg": True}, "Docker"),
        ({"docker": True, "testcontainers": False, "asyncpg": True}, "testcontainers"),
        ({"docker": True, "testcontainers": True, "asyncpg": False}, "asyncpg"),
    ):
        config = resolve_db_profile({"TEST_DB_PROFILE": "testcontainers"}, **kwargs)
        assert config.profile == PROFILE_SKIP
        assert expected.lower() in config.reason.lower()


def test_compose_profile(monkeypatch) -> None:
    import app.tests.utils.db as db_module

    monkeypatch.setattr(db_module, "detect_compose_postgres_url", lambda *args, **kwargs: None)
    config = resolve_db_profile(
        {"TEST_DB_PROFILE": "compose"}, compose_stack=True, asyncpg=True, docker=True
    )
    assert config.profile == PROFILE_COMPOSE
    assert config.url == DEFAULT_COMPOSE_DB_URL

    override = resolve_db_profile(
        {"TEST_DB_PROFILE": "compose", "TEST_COMPOSE_DB_URL": "postgresql+asyncpg://a/b"},
        compose_stack=True,
        asyncpg=True,
    )
    assert override.url == "postgresql+asyncpg://a/b"

    absent = resolve_db_profile({"TEST_DB_PROFILE": "compose"}, compose_stack=False, asyncpg=True)
    assert absent.profile == PROFILE_SKIP and "compose" in absent.reason


def test_auto_precedence() -> None:
    url = resolve_db_profile(
        {"TEST_DATABASE_URL": "postgresql+asyncpg://x/y"},
        docker=True,
        testcontainers=True,
        asyncpg=True,
    )
    assert url.profile == PROFILE_URL

    compose_first = resolve_db_profile(
        {}, docker=True, testcontainers=True, asyncpg=True, compose_stack=True
    )
    assert compose_first.profile == PROFILE_COMPOSE

    containers = resolve_db_profile(
        {}, docker=True, testcontainers=True, asyncpg=True, compose_stack=False
    )
    assert containers.profile == PROFILE_TESTCONTAINERS

    nothing = resolve_db_profile(
        {}, docker=False, testcontainers=False, asyncpg=False, compose_stack=False
    )
    assert nothing.profile == PROFILE_SKIP
    assert "no test database available" in nothing.reason


def test_invalid_profile_value_raises() -> None:
    with pytest.raises(ValueError, match="invalid TEST_DB_PROFILE"):
        resolve_db_profile({"TEST_DB_PROFILE": "sqlite"})


def test_describe_is_json_safe() -> None:
    config = DbProfileConfig(PROFILE_SKIP, None, "no db", True, False, False)
    payload = describe(config)
    assert payload == {
        "profile": PROFILE_SKIP,
        "available": False,
        "asyncpg": True,
        "docker": False,
        "testcontainers": False,
        "reason": "no db",
    }
    assert set(DB_PROFILES) == {"url", "testcontainers", "compose", "skip"}


def test_to_async_url_normalises_driver() -> None:
    assert to_async_url("postgresql://u:p@localhost:5432/db") == "postgresql+asyncpg://u:p@localhost:5432/db"
    assert (
        to_async_url("postgresql+psycopg2://u:p@localhost:5432/db")
        == "postgresql+asyncpg://u:p@localhost:5432/db"
    )
    assert to_async_url("postgresql+asyncpg://x/y") == "postgresql+asyncpg://x/y"


def test_container_host_port_parses_docker_port_output(monkeypatch) -> None:
    import subprocess as _subprocess

    from app.tests.utils.db import _container_host_port

    class _Result:
        returncode = 0
        stdout = "0.0.0.0:25432\n[::]:25432\n"
        stderr = ""

    monkeypatch.setattr(_subprocess, "run", lambda *args, **kwargs: _Result())
    assert _container_host_port("cid", "5432", 5) == "25432"

    class _Empty:
        returncode = 1
        stdout = ""
        stderr = "no such container"

    monkeypatch.setattr(_subprocess, "run", lambda *args, **kwargs: _Empty())
    assert _container_host_port("cid", "5432", 5) is None


def test_detect_compose_url_honours_explicit_env(monkeypatch) -> None:
    from app.tests.utils.db import detect_compose_postgres_url

    monkeypatch.setenv("TEST_COMPOSE_DB_URL", "postgresql+asyncpg://explicit@localhost:5555/db")
    assert detect_compose_postgres_url() == "postgresql+asyncpg://explicit@localhost:5555/db"
