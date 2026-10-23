"""FS-15 reporting query service: tenant/data-scope filtered, cursor-paged, read-only view queries."""

from __future__ import annotations

import base64
import csv
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.reporting.catalogue import (
    ENDPOINTS_BY_PATH,
    FILTER_COLUMNS,
    FILTER_TYPES,
    REPORT_ALIASES,
    VIEWS,
    VIEWS_BY_NAME,
    EndpointSpec,
)
from app.modules.reporting.views import ViewSpec

MAX_PAGE = 500
DEFAULT_PAGE = 100
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_SCOPE_TO_COLUMN = {"LOCATION": "location_id", "CLASS": "class_ref", "DEPARTMENT": "department_ref",
                    "SUBSIDIARY": "subsidiary_ref"}
_VIEW_SCHEMA_CACHE: dict[str, tuple[list[str], dict[str, str]]] = {}


class ReportingError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400,
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    return value


def encode_cursor(values: Sequence[Any]) -> str:
    payload = json.dumps([json_safe(v) for v in values], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(token: str | None, expected: int) -> list[Any] | None:
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ReportingError("REPORT_CURSOR_INVALID", "invalid cursor", details={"cursor": token}) from exc
    if not isinstance(values, list) or len(values) != expected:
        raise ReportingError("REPORT_CURSOR_INVALID", "cursor does not match the report ordering",
                             details={"cursor": token})
    return values


def allowed_filters(spec: EndpointSpec | ViewSpec | str) -> tuple[str, ...]:
    if isinstance(spec, EndpointSpec):
        return spec.filters
    name = spec if isinstance(spec, str) else spec.name
    for endpoint in ENDPOINTS_BY_PATH.values():
        if endpoint.view_name == name:
            return endpoint.filters
    return tuple(name for name in FILTER_COLUMNS)


def _all_view_filters(view: str) -> set[str]:
    return {k for k, v in FILTER_COLUMNS.items() if v in _column_names(view)}


def _column_names(view: str) -> set[str]:
    if view in _VIEW_SCHEMA_CACHE:
        return set(_VIEW_SCHEMA_CACHE[view][0])
    return set()


def _validate_identifier(value: str) -> str:
    if not IDENTIFIER.match(value):
        raise ReportingError("REPORT_FILTER_INVALID", f"invalid filter column {value!r}")
    return value


def _cast(value: str, type_name: str) -> str:
    mapping = {"uuid": "uuid", "date": "date", "timestamptz": "timestamptz", "numeric": "numeric",
               "bool": "boolean", "text": "text"}
    return f"CAST(:{value} AS {mapping.get(type_name, 'text')})"


@dataclass(slots=True)
class Page:
    items: list[dict[str, Any]]
    next_cursor: str | None
    freshness: dict[str, Any]
    total: int | None = None


class ReportService:
    def __init__(self, session: AsyncSession, *, tenant_id: str, app_user_id: str | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.app_user_id = app_user_id

    # ------------------------------------------------------------------ schema helpers -----
    async def _view_schema(self, view: str) -> tuple[list[str], dict[str, str]]:
        if view in _VIEW_SCHEMA_CACHE:
            return _VIEW_SCHEMA_CACHE[view]
        rows = await self.session.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'reporting' AND table_name = :view ORDER BY ordinal_position"
            ),
            {"view": view},
        )
        columns = [(str(row[0]), str(row[1])) for row in rows.all()]
        if not columns:
            # Materialised views are not exposed by information_schema; read pg_attribute instead so
            # materialised read models (V-04/V-11/V-12/V-16/V-21) are queryable through the service.
            matview_rows = await self.session.execute(
                text(
                    "SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS data_type "
                    "FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'reporting' AND c.relname = :view AND a.attnum > 0 AND NOT a.attisdropped "
                    "ORDER BY a.attnum"
                ),
                {"view": view},
            )
            columns = [(str(row[0]), str(row[1])) for row in matview_rows.all()]
        if not columns:
            raise ReportingError("REPORT_VIEW_UNAVAILABLE", f"reporting view {view!r} is not available",
                                 status_code=503)
        names = [name for name, _ in columns]
        types = {name: kind for name, kind in columns}
        _VIEW_SCHEMA_CACHE[view] = (names, types)
        return names, types

    async def _data_scope_columns(self) -> dict[str, list[str]]:
        if not self.app_user_id:
            return {}
        rows = await self.session.execute(
            text(
                "SELECT scope_type, scope_value FROM platform.user_data_scope "
                "WHERE tenant_id = CAST(:tenant AS uuid) AND user_id = CAST(:user AS uuid)"
            ),
            {"tenant": self.tenant_id, "user": self.app_user_id},
        )
        out: dict[str, list[str]] = {}
        for scope_type, scope_value in rows.all():
            out.setdefault(str(scope_type), []).append(str(scope_value))
        return out

    # ------------------------------------------------------------------ pagination ---------
    async def query(self, view: str, *, filters: Mapping[str, Any] | None = None, cursor: str | None = None,
                    limit: int = DEFAULT_PAGE, fields: str | Sequence[str] | None = None) -> Page:
        spec = VIEWS_BY_NAME.get(view)
        if spec is None:
            raise ReportingError("REPORT_NOT_FOUND", f"unknown report view {view!r}", status_code=404)
        columns, types = await self._view_schema(view)
        allowed_fields = self._parse_fields(fields, columns)
        filter_sql, params = await self._filters(spec, filters or {}, columns, types)
        order = [part.strip() for part in spec.order_key.split(",")]
        order = [_validate_identifier(part) for part in order]
        if any(part not in columns for part in order):
            order = ["tenant_id"] if "tenant_id" in columns else []
        order_sql = ", ".join(order) if order else ""
        cursor_values = decode_cursor(cursor, len(order)) if cursor and order else None
        if cursor_values:
            if len(order) == 1:
                filter_sql.append(f"{order[0]} > {_cast('cursor_0', _sql_type(types.get(order[0])))}")
            else:
                left = ", ".join(order)
                right = ", ".join(_cast(f"cursor_{i}", _sql_type(types.get(col)))
                                  for i, col in enumerate(order))
                filter_sql.append(f"({left}) > ({right})")
            for i, value in enumerate(cursor_values):
                params[f"cursor_{i}"] = value
        where = " AND ".join(s for s in filter_sql if s)
        select = ", ".join(allowed_fields) if allowed_fields else "*"
        sql = f"SELECT {select} FROM reporting.{view}"
        if where:
            sql += " WHERE " + where
        if order_sql:
            sql += " ORDER BY " + order_sql
        sql += f" LIMIT {max(1, min(int(limit), MAX_PAGE)) + 1}"
        result = await self.session.execute(text(sql), params)
        rows = [dict(row._mapping) for row in result.all()]
        next_cursor = None
        if len(rows) > int(limit):
            rows = rows[:int(limit)]
            last = rows[-1]
            next_cursor = encode_cursor([last.get(col) for col in order])
        items = [{k: json_safe(v) for k, v in row.items()} for row in rows]
        return Page(items=items, next_cursor=next_cursor, freshness=await self.get_freshness(view))

    async def _filters(self, spec: ViewSpec, filters: Mapping[str, Any], columns: list[str],
                       types: dict[str, str]) -> tuple[list[str], dict[str, Any]]:
        allowed = {name for name in FILTER_COLUMNS if FILTER_COLUMNS[name] in columns}
        clauses: list[str] = ["tenant_id = CAST(:tenant AS uuid)"] if "tenant_id" in columns else []
        params: dict[str, Any] = {"tenant": self.tenant_id}
        for public, raw in filters.items():
            if raw is None or raw == "":
                continue
            if public not in allowed:
                raise ReportingError("REPORT_FILTER_NOT_ALLOWED", f"filter {public!r} is not allowed for {spec.name}",
                                     status_code=422, details={"filter": public})
            column = FILTER_COLUMNS[public]
            kind = FILTER_TYPES.get(public, types.get(column, "text"))
            param = f"f_{public}"
            if public in {"date_from", "from", "required_by_from", "occurred_from", "layer_date_from",
                          "period_from", "bucket_from"}:
                clauses.append(f"{column} >= {_cast(param, kind)}")
            elif public in {"date_to", "to", "required_by_to", "occurred_to", "layer_date_to",
                            "period_to", "bucket_to", "expiry_before"}:
                clauses.append(f"{column} <= {_cast(param, kind)}")
            elif public == "include_estimated":
                clauses.append(f"estimated_negative = CAST({param} AS boolean)" if _is_true(raw) else "TRUE")
            elif public == "include_expired":
                clauses.append("TRUE" if _is_true(raw) else "(expiry_date IS NULL OR expiry_date >= CURRENT_DATE)")
            elif public == "shortage_only":
                clauses.append("shortage_qty > 0" if _is_true(raw) else "TRUE")
            elif public == "serial_number":
                clauses.append(f"serial_number = {_cast(param, kind)}")
            else:
                clauses.append(f"{column} = {_cast(param, kind)}")
            params[param] = str(raw).lower() if kind == "bool" else raw
        scope = await self._data_scope_columns()
        for scope_type, values in scope.items():
            scope_col = _SCOPE_TO_COLUMN.get(scope_type)
            if scope_col and scope_col in columns and values:
                param = f"scope_{scope_type.lower()}"
                clauses.append(f"{scope_col} = ANY(CAST({param} AS text[]))")
                params[param] = values
        return clauses, params

    def _parse_fields(self, fields: str | Sequence[str] | None, columns: list[str]) -> list[str]:
        if not fields:
            return list(columns)
        names = [f.strip() for f in (fields.split(",") if isinstance(fields, str) else fields) if f.strip()]
        if not names:
            return list(columns)
        unknown = [name for name in names if name not in columns]
        if unknown:
            raise ReportingError("REPORT_FIELD_NOT_ALLOWED", "unknown or non-allow-listed field(s)",
                                 status_code=422, details={"fields": unknown, "allowed": columns})
        return names

    # ------------------------------------------------------------------ freshness ----------
    async def get_freshness(self, view: str) -> dict[str, Any]:
        spec = VIEWS_BY_NAME.get(view)
        if spec is None:
            raise ReportingError("REPORT_NOT_FOUND", f"unknown report view {view!r}", status_code=404)
        last_refresh: Any = None
        status = "UNKNOWN"
        try:
            result = await self.session.execute(
                text(
                    "SELECT r.status, r.finished_at FROM platform.job_run r "
                    "JOIN platform.job_definition d ON d.tenant_id = r.tenant_id AND d.id = r.job_definition_id "
                    "WHERE r.tenant_id = CAST(:tenant AS uuid) AND d.name = 'reporting.refresh_views' "
                    "ORDER BY r.finished_at DESC NULLS LAST LIMIT 1"
                ),
                {"tenant": self.tenant_id},
            )
            row = result.first()
            if row:
                status = str(row[0])
                last_refresh = json_safe(row[1])
        except Exception:  # freshness is best-effort; a missing job history must not break reports
            status = "UNKNOWN"
        return {
            "view_name": view,
            "materialized": spec.materialized,
            "last_refreshed_at": last_refresh,
            "refresh_status": status,
            "max_staleness_seconds": 900 if spec.materialized else 0,
            "degraded": spec.materialized and last_refresh is None,
        }

    # ------------------------------------------------------------------ catalogue ----------
    async def catalogue(self) -> dict[str, Any]:
        items = []
        for spec in VIEWS:
            items.append({
                "view_name": spec.name, "view_number": spec.number, "purpose": spec.purpose,
                "materialized": spec.materialized, "cost_visible": spec.cost_visible,
                "order_key": spec.order_key, "freshness": await self.get_freshness(spec.name),
            })
        return {"contract": "reporting.readmodels", "version": "1.0.0", "items": items, "total": len(items)}

    # ------------------------------------------------------------------ export -------------
    async def export_view(self, report_name: str, *, filters: Mapping[str, Any] | None = None,
                          cursor: str | None = None, limit: int = 500,
                          format: str = "json", fields: str | None = None) -> tuple[str, str]:
        view = REPORT_ALIASES.get(report_name)
        if view is None:
            raise ReportingError("REPORT_NOT_FOUND", f"unknown report {report_name!r}", status_code=404)
        page = await self.query(view, filters=filters, cursor=cursor, limit=limit, fields=fields)
        if format == "csv":
            output = io.StringIO()
            rows = page.items
            if rows:
                writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            return output.getvalue(), "text/csv"
        return json.dumps({"items": page.items, "next_cursor": page.next_cursor,
                           "freshness": page.freshness}), "application/json"


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _sql_type(kind: str | None) -> str:
    return {"uuid": "uuid", "date": "date", "timestamp with time zone": "timestamptz",
            "numeric": "numeric", "integer": "integer", "bigint": "bigint",
            "boolean": "boolean"}.get(kind or "", "text")


__all__ = ["ReportService", "ReportingError", "Page", "decode_cursor", "encode_cursor", "json_safe",
           "allowed_filters", "MAX_PAGE", "DEFAULT_PAGE"]
