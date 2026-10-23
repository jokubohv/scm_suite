"""Unit: tenant provisioning registry consistency (FS-00 residual task-77)."""

from __future__ import annotations

from app.core.tenant_provisioning import (
    INVENTORY_COUNT_APPROVAL_DEFINITION,
    TENANT_FLAG_DEFAULTS,
    TENANT_SEQUENCE_DEFAULTS,
    TENANT_SETTING_DEFAULTS,
    expected_default_counts,
)
from app.seed.constants import DOCUMENTED_NUMBER_SEQUENCES

ALLOWED_NAMESPACES = {
    "inventory", "manufacturing", "wms", "replenishment", "costing", "pricing", "platform",
    "accounting", "rbac", "auth", "counts", "traceability", "procurement", "quality", "consigned",
    "extensions", "reporting",
}


def test_expected_counts_are_stable() -> None:
    counts = expected_default_counts()
    assert counts == {"sequences": 38, "settings": 136, "flags": 26, "approval_definitions": 1}
    assert len(TENANT_SEQUENCE_DEFAULTS) == 38
    assert len(TENANT_SETTING_DEFAULTS) == 136
    assert len(TENANT_FLAG_DEFAULTS) == 26


def test_sequences_are_unique_and_cover_new_doc_types() -> None:
    doc_types = [row[0] for row in TENANT_SEQUENCE_DEFAULTS]
    assert len(doc_types) == len(set(doc_types))
    for expected in ("PO", "IR", "VB", "VC", "VRA", "WO", "WIP", "AB", "AU", "ECN", "MFGSCH",
                     "DP", "SP", "ALC", "AIM", "ASN", "RCV", "WAVE", "SHIP", "MFST", "RPL", "TASK",
                     "INSP", "COA", "CAG", "CTR", "WR", "WC", "REP", "CNT"):
        assert expected in doc_types, expected
    assert set(DOCUMENTED_NUMBER_SEQUENCES) <= set(doc_types)
    for doc_type, prefix, padding in TENANT_SEQUENCE_DEFAULTS:
        assert doc_type and prefix and int(padding) >= 1


def test_settings_cover_new_namespaces_and_prefixes() -> None:
    keys = [(namespace, key) for namespace, key, _value, _type in TENANT_SETTING_DEFAULTS]
    assert len(keys) == len(set(keys))
    for namespace, _key, _value, _type in TENANT_SETTING_DEFAULTS:
        assert namespace in ALLOWED_NAMESPACES
    for expected in (
        ("inventory", "numbered.enabled"), ("inventory", "counts.tolerance_qty"),
        ("inventory", "procurement.match.require_3way"), ("inventory", "replenishment.horizon_days"),
        ("inventory", "wms.max_lines_per_wave"), ("inventory", "quality.hold_status_code"),
        ("inventory", "consigned.payment_trigger_default"), ("inventory", "extensions.upc.auto_assign"),
        ("manufacturing", "bom.max_depth"),
    ):
        assert expected in keys, expected


def test_flags_are_deploy_dark_and_cover_new_flags() -> None:
    assert len(TENANT_FLAG_DEFAULTS) == len(set(TENANT_FLAG_DEFAULTS))
    for expected in ("procurement.multiple_vendors_enabled", "consigned.assembly_incompatible_guard", "extensions.upc.auto_assign"):
        assert expected in TENANT_FLAG_DEFAULTS
    # every declared FS-00 seed flag is included in provisioning
    import json

    from app.core.flags import SEED_FLAGS_PATH
    declared = {row["flag_key"] for row in json.loads(SEED_FLAGS_PATH.read_text())["records"]}
    assert declared <= set(TENANT_FLAG_DEFAULTS)
    assert "inventory.counts.enabled" in TENANT_FLAG_DEFAULTS


def test_default_inventory_count_approval_is_enabled_single_step() -> None:
    definition = INVENTORY_COUNT_APPROVAL_DEFINITION
    assert definition["is_enabled"] is True
    assert definition["condition"] == {}
    steps = definition["steps"]
    assert len(steps) == 1
    assert steps[0]["step"] == 1
    assert steps[0]["mode"] == "SEQUENTIAL"
    assert steps[0]["required_approvals"] == 1
    assert steps[0]["roles"]
