"""Data-driven, idempotent seed loader (S0-07).

Load order is fixed by ``app/seed/data/manifest.json``; every dataset is upserted by natural key with
no destructive deletes. The same loader is used by tests, the CLI and (later) FS-00 provisioning via
:class:`app.seed.repository.SqlAlchemySeedRepository`.

Idempotency contract: loading the same seed revision twice against the same repository produces
``inserted == 0`` and ``updated == 0`` on the second run and an identical :meth:`state_digest`.
Dry-run runs compute exactly the changes a real run would perform without writing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from app.seed.constants import (
    DEFAULT_DATA_DIR,
    DEFAULT_PERMISSIONS_SOURCE,
    DOCUMENTED_NUMBER_SEQUENCES,
    DOCUMENTED_TRANSACTION_TYPES,
    EXPECTED_CATALOGUE_COUNTS,
    EXPECTED_DATASET_COUNT,
    SEED_SCHEMA_VERSION,
    SYSTEM_TENANT_ID,
    TENANT_SCOPE_SYSTEM,
    TENANT_SCOPE_TENANT,
)
from app.seed.permissions import PermissionCatalogue, SeedValidationError, load_permissions
from app.seed.report import DatasetReport, SeedReport
from app.seed.repository import SeedRepository


class SeedLoader:
    """Loads the S0 seed manifests into a :class:`SeedRepository`."""

    def __init__(
        self,
        repository: SeedRepository,
        *,
        data_dir: str | Path | None = None,
        permissions_source: str | Path | None = None,
        dry_run: bool = False,
    ) -> None:
        self.repository = repository
        self.data_dir = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
        self._permissions_source = permissions_source if permissions_source is not None else DEFAULT_PERMISSIONS_SOURCE
        self.dry_run = dry_run
        self._catalogue: PermissionCatalogue | None = None
        self._manifest: dict[str, Any] | None = None

    # ---- public API -----------------------------------------------------------
    def load(self, tenant_id: str | UUID, *, seed_system: bool = True) -> SeedReport:
        """Apply the seed manifest for one target tenant.

        ``tenant_id`` may be a UUID or UUID string. When it equals :data:`SYSTEM_TENANT_ID` only
        global/system datasets are loaded (tenant-scoped datasets would be meaningless).
        """
        target = _coerce_tenant_id(tenant_id)
        manifest = self.manifest
        catalogue = self.catalogue
        report = SeedReport(
            tenant_id=str(target),
            dry_run=self.dry_run,
            plan_version=str(manifest.get("plan_version", "")),
            seed_schema_version=SEED_SCHEMA_VERSION,
            digest_before=self.repository.state_digest(),
        )
        report.warnings.extend(catalogue.warnings)
        _validate_manifest(manifest, self.data_dir, report)

        system_only = target == SYSTEM_TENANT_ID
        for dataset in manifest["datasets"]:
            if system_only and dataset["tenant_scope"] != TENANT_SCOPE_SYSTEM:
                continue
            if not seed_system and dataset["tenant_scope"] == TENANT_SCOPE_SYSTEM:
                continue
            ds_report = self._apply_dataset(dataset, target, catalogue, system_only=system_only)
            report.datasets.append(ds_report)
            if ds_report.errors:
                report.errors.extend(f"{dataset['name']}: {error}" for error in ds_report.errors)

        report.digest_after = self.repository.state_digest()
        return report

    # ---- lazy sources ---------------------------------------------------------
    @property
    def manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            path = self.data_dir / "manifest.json"
            if not path.is_file():
                raise SeedValidationError(f"seed manifest not found at {path}")
            self._manifest = json.loads(path.read_text(encoding="utf-8"))
            _validate_manifest(self._manifest, self.data_dir, SeedReport(tenant_id="", dry_run=False))
        return self._manifest

    @property
    def catalogue(self) -> PermissionCatalogue:
        if self._catalogue is None:
            self._catalogue = load_permissions(self._permissions_source)
        return self._catalogue

    # ---- dataset application --------------------------------------------------
    def _apply_dataset(
        self,
        dataset: Mapping[str, Any],
        tenant_id: UUID,
        catalogue: PermissionCatalogue,
        *,
        system_only: bool,
    ) -> DatasetReport:
        report = DatasetReport(name=dataset["name"], table=dataset["table"])
        try:
            rows = sorted(self._dataset_rows(dataset["name"], tenant_id, catalogue), key=lambda item: item[0])
        except (SeedValidationError, KeyError, ValueError) as exc:
            report.errors.append(str(exc))
            return report

        for key, values in rows:
            try:
                existing = self.repository.get(dataset["table"], key)
            except Exception as exc:  # repository errors surface as dataset errors, never partial silent runs
                report.errors.append(f"read failed for key {key}: {exc}")
                return report
            if existing is None:
                report.inserted += 1
                if not self.dry_run:
                    self.repository.upsert(dataset["table"], key, values)
            elif _canonical_values(existing) != _canonical_values(values):
                report.updated += 1
                if not self.dry_run:
                    self.repository.upsert(dataset["table"], key, values)
            else:
                report.unchanged += 1
        return report

    def _dataset_rows(
        self, dataset_name: str, tenant_id: UUID, catalogue: PermissionCatalogue
    ) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        scope = _dataset_scope(self.manifest, dataset_name)
        is_tenant = scope == TENANT_SCOPE_TENANT and tenant_id != SYSTEM_TENANT_ID
        tenant_value: str | None = str(tenant_id) if is_tenant else None

        if dataset_name == "system_tenant":
            rows = _expect_list(self._data("system_tenant.json")["records"], "system_tenant.records")
            return [((str(row["id"]),), dict(row)) for row in rows]

        if dataset_name == "currencies":
            rows = _expect_list(self._data("currencies.json")["records"], "currencies.records")
            currencies_out: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            codes: list[str] = []
            for row in rows:
                row = dict(row)
                codes.append(row["code"])
                row["tenant_id"] = None
                currencies_out.append((("SYSTEM", row["code"]), row))
            if len(set(codes)) != len(codes):
                raise SeedValidationError("duplicate currency codes in seed data")
            return currencies_out

        if dataset_name == "permissions":
            return [
                ((str(tenant_value), row["scope"]), {**row, "tenant_id": tenant_value})
                for row in catalogue.permission_rows()
            ]

        if dataset_name == "role_templates":
            return [
                ((str(tenant_value), row["code"]), {**row, "tenant_id": tenant_value})
                for row in catalogue.role_rows()
            ]

        if dataset_name == "role_permissions":
            permission_scopes = catalogue.permission_by_scope
            role_permissions_out: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            for row in catalogue.role_permission_rows():
                if row["scope"] not in permission_scopes:
                    raise SeedValidationError(f"role permission references unknown scope {row['scope']!r}")
                role_permissions_out.append(
                    (
                        (str(tenant_value), row["role_code"], row["scope"]),
                        {**row, "tenant_id": tenant_value},
                    )
                )
            return role_permissions_out

        if dataset_name == "sod_policy":
            return [
                ((str(tenant_value), row["a"], row["b"]), {**row, "tenant_id": tenant_value})
                for row in catalogue.sod_rows()
            ]

        if dataset_name == "tenant_settings":
            rows = _expect_list(self._data("tenant_settings.json")["records"], "tenant_settings.records")
            return [
                (
                    (str(tenant_value), row["namespace"], row["key"]),
                    {**row, "tenant_id": tenant_value},
                )
                for row in rows
            ]

        if dataset_name == "feature_flags":
            rows = _expect_list(self._data("feature_flags.json")["records"], "feature_flags.records")
            feature_flags_out: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            for row in rows:
                if not row["flag_key"].endswith(".enabled"):
                    raise SeedValidationError(f"feature flag {row['flag_key']!r} must end with '.enabled'")
                if row.get("default_enabled") is not False:
                    raise SeedValidationError(f"feature flag {row['flag_key']!r} must default OFF")
                feature_flags_out.append(
                    (
                        (str(tenant_value), row["flag_key"]),
                        {
                            **row,
                            "tenant_id": tenant_value,
                            "is_enabled": False,
                            "source": "DEFAULT",
                        },
                    )
                )
            return feature_flags_out

        if dataset_name == "number_sequences":
            rows = _expect_list(self._data("number_sequences.json")["records"], "number_sequences.records")
            doc_types = [row["doc_type"] for row in rows]
            if set(doc_types) != set(DOCUMENTED_NUMBER_SEQUENCES):
                missing = sorted(set(DOCUMENTED_NUMBER_SEQUENCES) - set(doc_types))
                extra = sorted(set(doc_types) - set(DOCUMENTED_NUMBER_SEQUENCES))
                raise SeedValidationError(f"number sequence doc_types mismatch (missing={missing}, extra={extra})")
            return [
                (
                    (str(tenant_value), row["doc_type"], str(row.get("scope_key", "TENANT")), ""),
                    {
                        **row,
                        "tenant_id": tenant_value,
                        "next_value": 1,
                        "period_key": None,
                    },
                )
                for row in rows
            ]

        if dataset_name == "transaction_types":
            rows = _expect_list(self._data("transaction_types.json")["records"], "transaction_types.records")
            codes = [row["code"] for row in rows]
            if set(codes) != set(DOCUMENTED_TRANSACTION_TYPES):
                missing = sorted(set(DOCUMENTED_TRANSACTION_TYPES) - set(codes))
                raise SeedValidationError(f"transaction types mismatch (missing={missing})")
            transaction_types_out: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            for row in rows:
                row = dict(row)
                row["tenant_id"] = None
                transaction_types_out.append((("SYSTEM", row["code"]), row))
            return transaction_types_out

        if dataset_name == "uom_types":
            rows = _expect_list(self._data("uom.json")["records"]["uom_types"], "uom.uom_types")
            return [((str(tenant_value), row["code"]), {**row, "tenant_id": tenant_value}) for row in rows]

        if dataset_name == "uom_units":
            payload = self._data("uom.json")["records"]
            types = {row["code"]: row for row in _expect_list(payload["uom_types"], "uom.uom_types")}
            units = _expect_list(payload["uom_units"], "uom.uom_units")
            uom_units_out: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            seen_base: dict[str, str] = {}
            for row in units:
                uom_type = types.get(row["uom_type_code"])
                if uom_type is None:
                    raise SeedValidationError(f"uom unit references unknown type {row['uom_type_code']!r}")
                if row["is_base"]:
                    if row["code"] != uom_type["base_uom_code"]:
                        raise SeedValidationError(
                            f"uom unit {row['code']!r} marked base but type base is {uom_type['base_uom_code']!r}"
                        )
                    if row["uom_type_code"] in seen_base:
                        raise SeedValidationError(f"uom type {row['uom_type_code']!r} has multiple base units")
                    seen_base[row["uom_type_code"]] = row["code"]
                uom_units_out.append(
                    (
                        (str(tenant_value), row["uom_type_code"], row["code"]),
                        {**row, "tenant_id": tenant_value},
                    )
                )
            return uom_units_out

        if dataset_name == "inventory_statuses":
            rows = _expect_list(
                self._data("inventory_statuses.json")["records"], "inventory_statuses.records"
            )
            defaults = [row for row in rows if row.get("is_default")]
            if len(defaults) != 1:
                raise SeedValidationError(f"inventory statuses must have exactly one default, found {len(defaults)}")
            if defaults[0].get("is_consigned"):
                raise SeedValidationError("default inventory status must not be consigned")
            return [
                ((str(tenant_value), row["code"]), {**row, "tenant_id": tenant_value}) for row in rows
            ]

        raise SeedValidationError(f"unknown seed dataset {dataset_name!r}")

    # ---- helpers --------------------------------------------------------------
    def _data(self, relative_path: str) -> dict[str, Any]:
        path = self.data_dir / relative_path
        if not path.is_file():
            raise SeedValidationError(f"seed data file not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))


# ---- module-level helpers -----------------------------------------------------
def _coerce_tenant_id(tenant_id: str | UUID) -> UUID:
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(str(tenant_id))
    except (TypeError, ValueError) as exc:
        raise SeedValidationError(f"invalid tenant id {tenant_id!r}: expected UUID") from exc


def _dataset_scope(manifest: Mapping[str, Any], name: str) -> str:
    for dataset in manifest["datasets"]:
        if dataset["name"] == name:
            return str(dataset["tenant_scope"])
    raise SeedValidationError(f"dataset {name!r} missing from manifest")


def _expect_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise SeedValidationError(f"{label} must be a non-empty list")
    for row in value:
        if not isinstance(row, dict):
            raise SeedValidationError(f"{label} contains a non-object row")
    return value


def _canonical_values(values: Mapping[str, Any]) -> str:
    return json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _validate_manifest(manifest: Mapping[str, Any], data_dir: Path, report: SeedReport) -> None:
    datasets = manifest.get("datasets")
    if not isinstance(datasets, list) or len(datasets) != EXPECTED_DATASET_COUNT:
        report.errors.append(
            f"seed manifest must declare {EXPECTED_DATASET_COUNT} datasets, found {len(datasets or [])}"
        )
        return
    names = [dataset.get("name") for dataset in datasets]
    if len(set(names)) != len(names):
        report.errors.append(f"duplicate seed dataset names: {sorted({n for n in names if names.count(n) > 1})}")
    tables = [dataset.get("table") for dataset in datasets]
    if len(set(tables)) != len(tables):
        report.errors.append("seed datasets must target distinct tables")
    for dataset in datasets:
        for key_field in ("name", "table", "key", "tenant_scope", "owner_feature_set", "source"):
            if not dataset.get(key_field):
                report.errors.append(f"dataset {dataset.get('name')!r} missing {key_field!r}")
        scope = dataset.get("tenant_scope")
        if scope not in (TENANT_SCOPE_TENANT, TENANT_SCOPE_SYSTEM):
            report.errors.append(f"dataset {dataset.get('name')!r} has invalid tenant_scope {scope!r}")
        source = str(dataset.get("source", "")).split("#", 1)[0]
        if source.startswith("data/"):
            path = data_dir / source[len("data/") :]
            if not path.is_file():
                report.errors.append(f"dataset {dataset.get('name')!r} source file missing: {path}")
    if manifest.get("plan_version") in (None, ""):
        report.errors.append("seed manifest must declare plan_version")


def validate_sources(
    *,
    data_dir: str | Path | None = None,
    permissions_source: str | Path | None = None,
) -> list[str]:
    """Validate seed payloads and the RBAC catalogue without touching a repository.

    Used by tests and by the loader before applying rows; returns a list of human-readable errors.
    """
    errors: list[str] = []
    loader = SeedLoader(
        repository=_NoopRepository(),
        data_dir=data_dir,
        permissions_source=permissions_source,
        dry_run=True,
    )
    probe = SeedReport(tenant_id="probe", dry_run=True)
    try:
        manifest = loader.manifest
    except SeedValidationError as exc:
        return [str(exc)]
    _validate_manifest(manifest, loader.data_dir, probe)
    errors.extend(probe.errors)
    try:
        catalogue = loader.catalogue
    except SeedValidationError as exc:
        errors.append(str(exc))
        return errors
    counts = catalogue.counts()
    for key, expected in EXPECTED_CATALOGUE_COUNTS.items():
        if counts.get(key) != expected:
            errors.append(f"catalogue {key}={counts.get(key)} expected {expected}")
    # Exercise every dataset row builder with a probe tenant so referential checks run here too.
    probe_tenant = UUID("00000000-0000-7000-8000-000000000001")
    for dataset in manifest.get("datasets", []):
        try:
            rows = loader._dataset_rows(dataset["name"], probe_tenant, catalogue)  # noqa: SLF001 - internal validation
        except SeedValidationError as exc:
            errors.append(f"{dataset['name']}: {exc}")
            continue
        if not rows:
            errors.append(f"{dataset['name']}: no seed rows")
            continue
        keys = [key for key, _ in rows]
        if len(set(keys)) != len(keys):
            errors.append(f"{dataset['name']}: duplicate natural keys")
    return errors


class _NoopRepository:
    """Repository stub used by source validation (never written to)."""

    def get(self, table: str, key: tuple[str, ...]) -> dict[str, Any] | None:
        return None

    def upsert(self, table: str, key: tuple[str, ...], values: Mapping[str, Any]) -> Any:
        raise AssertionError("source validation must not write")

    def count(self, table: str) -> int:
        return 0

    def tables(self) -> Iterable[str]:
        return []

    def state_digest(self) -> str:
        return "noop"


def summarise(report: SeedReport) -> str:
    """Short one-line summary used by CLI wrappers/tests."""
    return (
        f"tenant={report.tenant_id} dry_run={report.dry_run} valid={report.valid} "
        f"inserted={report.inserted} updated={report.updated} unchanged={report.unchanged}"
    )
