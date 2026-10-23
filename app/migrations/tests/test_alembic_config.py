"""Static checks for the Alembic scaffold (no database required)."""

from __future__ import annotations

import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _ini() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    read = parser.read(ALEMBIC_INI, encoding="utf-8")
    assert read, f"cannot read {ALEMBIC_INI}"
    return parser


def test_alembic_ini_points_at_migrations_package() -> None:
    parser = _ini()
    assert parser.get("alembic", "script_location") == "app/migrations"
    assert parser.get("alembic", "file_template") == "%(rev)s_%(slug)s"


def test_alembic_ini_contains_no_database_credentials() -> None:
    parser = _ini()
    assert parser.get("alembic", "sqlalchemy.url", fallback="") == ""
    text = ALEMBIC_INI.read_text(encoding="utf-8")
    lowered = text.lower()
    for marker in ("password=", "postgresql://", "postgres://", "asyncpg://"):
        assert marker not in lowered, f"secret-bearing URL marker {marker!r} found in alembic.ini"


def test_versions_directory_holds_a_valid_chain() -> None:
    from app.migrations.chain_guard import validate_chain

    versions = REPO_ROOT / "app" / "migrations" / "versions"
    assert versions.is_dir()
    # S0 baseline is empty; after FS-00 lands 0001–0003 this still passes because the chain is linear.
    result = validate_chain(versions)
    assert result.ok, result.errors


def test_env_wires_async_engine_and_url_resolution() -> None:
    env_py = (REPO_ROOT / "app" / "migrations" / "env.py").read_text(encoding="utf-8")
    assert "async_engine_from_config" in env_py
    assert "asyncpg" in env_py
    assert "ALEMBIC_DATABASE_URL" in env_py
    assert "DATABASE_URL" in env_py
    assert "process_revision_directives" in env_py


def test_script_py_mako_documents_chain_rules() -> None:
    template = (REPO_ROOT / "app" / "migrations" / "script.py.mako").read_text(encoding="utf-8")
    assert "revision: str = ${repr(up_revision)}" in template
    assert "down_revision: str | None" in template
    assert "chain" in template.lower()
