"""Deterministic seed-run report objects (no I/O, JSON/CLI friendly)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DatasetReport:
    """Per-dataset outcome of one seed run."""

    name: str
    table: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_reference_only: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged + self.skipped_reference_only

    @property
    def changed(self) -> int:
        return self.inserted + self.updated

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "table": self.table,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped_reference_only": self.skipped_reference_only,
            "total": self.total,
            "errors": list(self.errors),
        }


@dataclass
class SeedReport:
    """Outcome of a complete seed run for one target tenant."""

    tenant_id: str
    dry_run: bool
    plan_version: str = ""
    seed_schema_version: str = ""
    datasets: list[DatasetReport] = field(default_factory=list)
    digest_before: str = ""
    digest_after: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ---- aggregate properties -------------------------------------------------
    @property
    def valid(self) -> bool:
        return not self.errors and all(not ds.errors for ds in self.datasets)

    @property
    def inserted(self) -> int:
        return sum(ds.inserted for ds in self.datasets)

    @property
    def updated(self) -> int:
        return sum(ds.updated for ds in self.datasets)

    @property
    def unchanged(self) -> int:
        return sum(ds.unchanged for ds in self.datasets)

    @property
    def skipped_reference_only(self) -> int:
        return sum(ds.skipped_reference_only for ds in self.datasets)

    @property
    def total(self) -> int:
        return sum(ds.total for ds in self.datasets)

    @property
    def changed(self) -> int:
        return sum(ds.changed for ds in self.datasets)

    @property
    def digest_unchanged(self) -> bool:
        """True when applying the run would not change repository state (idempotent load)."""
        return self.digest_before == self.digest_after and self.changed == 0

    def dataset(self, name: str) -> DatasetReport:
        for ds in self.datasets:
            if ds.name == name:
                return ds
        raise KeyError(f"dataset {name!r} not in report")

    # ---- serialisation --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "dry_run": self.dry_run,
            "plan_version": self.plan_version,
            "seed_schema_version": self.seed_schema_version,
            "valid": self.valid,
            "digest_before": self.digest_before,
            "digest_after": self.digest_after,
            "digest_unchanged": self.digest_unchanged,
            "totals": {
                "datasets": len(self.datasets),
                "inserted": self.inserted,
                "updated": self.updated,
                "unchanged": self.unchanged,
                "skipped_reference_only": self.skipped_reference_only,
                "total": self.total,
                "changed": self.changed,
            },
            "datasets": [ds.to_dict() for ds in self.datasets],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, ensure_ascii=False)

    def render(self) -> str:
        """Human-readable multi-line summary (deterministic ordering)."""
        lines = [
            f"seed run: tenant={self.tenant_id} dry_run={self.dry_run} valid={self.valid} "
            f"plan_version={self.plan_version}",
            f"  digest: {self.digest_before} -> {self.digest_after}"
            f" ({'unchanged' if self.digest_unchanged else 'CHANGED'})",
            f"  totals: inserted={self.inserted} updated={self.updated} unchanged={self.unchanged} "
            f"reference_only={self.skipped_reference_only} total={self.total}",
        ]
        for ds in self.datasets:
            lines.append(
                f"    - {ds.name:<22} {ds.table:<28} +{ds.inserted:<4} ~{ds.updated:<4} "
                f"={ds.unchanged:<4} ref={ds.skipped_reference_only:<3}"
            )
        for warning in self.warnings:
            lines.append(f"  WARN: {warning}")
        for ds in self.datasets:
            for error in ds.errors:
                lines.append(f"  ERROR [{ds.name}]: {error}")
        for error in self.errors:
            lines.append(f"  ERROR: {error}")
        return "\n".join(lines)
