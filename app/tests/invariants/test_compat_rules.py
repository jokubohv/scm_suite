"""INV: compatibility rule engine determinism/ordering/enforcement (Δ-CFG RULE-MFG-26/27/29/31).

Pure-engine invariants, no database: dual scope AND semantics, scope specificity then priority then code,
rule severity > rule-set mode > tenant default, HARD blocks unless a valid override stamps it OVERRIDDEN,
every rule type, capacity arithmetic and configuration-hash determinism/input-order independence.
"""

from __future__ import annotations

from decimal import Decimal

from app.modules.manufacturing import compat


def _components() -> list[dict]:
    return [
        {"item_id": "cpu", "item_number": "CPU-1", "qty": "1", "attributes": {"socket": "AM5"}, "slots": {"dimm": 2}},
        {"item_id": "mb", "item_number": "MB-1", "qty": "1", "attributes": {"socket": "AM5"}, "slots": {"dimm": 1}},
        {"item_id": "psu", "item_number": "PSU-1", "qty": "1", "attributes": {"watts": "750"}},
    ]


def _rule_set(
    code: str,
    scope: str,
    rules: tuple[compat.RuleView, ...],
    *,
    priority: int = 100,
    mode: str = "INHERIT",
    status: str = "ACTIVE",
) -> compat.RuleSetView:
    return compat.RuleSetView(
        code=code, scope_type=scope, priority=priority, enforcement_mode=mode, rules=rules, status=status
    )


def _rule(
    code: str,
    rule_type: str,
    *,
    constraint: dict | None = None,
    subject: dict | None = None,
    subject_type: str = "ITEM_SET",
    severity: str = "INHERIT",
    priority: int = 100,
) -> compat.RuleView:
    return compat.RuleView(
        code=code,
        rule_type=rule_type,
        subject_type=subject_type,
        subject_expr=subject or {},
        constraint_expr=constraint or {},
        severity=severity,
        priority=priority,
    )


def test_rule_severity_beats_rule_set_mode_beats_default() -> None:
    failing = {"left_items": ["cpu"], "right_items": ["mb"]}
    hard_rule = _rule("R-HARD", "EXCLUDES", constraint=failing, severity="HARD")
    warn_rule = _rule("R-WARN", "EXCLUDES", constraint=failing, severity="WARN")
    sets = [
        _rule_set("ASSEMBLY-HARD", "ASSEMBLY", (hard_rule,), mode="WARN"),
        _rule_set("CUSTOMER-WARN", "CUSTOMER", (warn_rule,), mode="HARD"),
    ]
    result = compat.evaluate(sets, components=_components(), tenant_default="WARN")
    assert result.result == "HARD_FAIL"
    assert result.highest_enforcement == "HARD"
    by_code = {outcome.rule_code: outcome for outcome in result.outcomes}
    assert by_code["R-HARD"].enforcement == "HARD"
    assert by_code["R-WARN"].enforcement == "WARN"

    # Rule severity WARN > rule-set INHERIT: warning regardless of tenant default.
    default_only = [_rule_set("ASSEMBLY-INHERIT", "ASSEMBLY", (warn_rule,), mode="INHERIT")]
    assert compat.evaluate(default_only, components=_components(), tenant_default="HARD").result == "WARN"
    # INHERIT severity falls through to rule-set mode, then the tenant default.
    inherit_rule = _rule("R-INHERIT", "EXCLUDES", constraint=failing, severity="INHERIT")
    inherit_set = [_rule_set("ASSEMBLY-INHERIT2", "ASSEMBLY", (inherit_rule,), mode="INHERIT")]
    assert compat.evaluate(inherit_set, components=_components(), tenant_default="HARD").result == "HARD_FAIL"
    assert compat.evaluate(inherit_set, components=_components(), tenant_default="WARN").result == "WARN"


def test_scope_specificity_then_priority_then_code_order_and_override() -> None:
    failing = {"left_items": ["cpu"], "right_items": ["mb"]}
    order = [
        _rule_set(
            "ZZ-ASSEMBLY", "ASSEMBLY", (_rule("A1", "EXCLUDES", constraint=failing, severity="HARD"),), priority=1
        ),
        _rule_set(
            "AA-CUSTOMER", "CUSTOMER", (_rule("C1", "EXCLUDES", constraint=failing, severity="WARN"),), priority=999
        ),
        _rule_set("MM-ORDER", "ORDER", (_rule("O1", "EXCLUDES", constraint=failing, severity="WARN"),), priority=50),
    ]
    result = compat.evaluate(order, components=_components())
    assert [item["code"] for item in result.matched_rule_sets] == ["MM-ORDER", "AA-CUSTOMER", "ZZ-ASSEMBLY"]
    assert result.result == "HARD_FAIL"
    overridden = compat.evaluate(order, components=_components(), override=True)
    assert overridden.result == "OVERRIDDEN"
    assert not overridden.blocked
    compat.require_acceptable(overridden, override=True)
    # Determinism is independent of input order.
    shuffled = compat.evaluate(list(reversed(order)), components=_components())
    assert shuffled.result == result.result
    assert [item["code"] for item in shuffled.matched_rule_sets] == [item["code"] for item in result.matched_rule_sets]


def test_rule_type_semantics_and_capacity_math() -> None:
    components = _components()
    slots = [{"slot_code": "dimm", "capacity": "4", "units_per_component": 1}]
    cases = {
        "REQUIRES": (
            _rule("REQ", "REQUIRES", constraint={"attribute": "socket", "value": "AM5"}, subject_type="ATTRIBUTE"),
            True,
        ),
        "EXCLUDES": (_rule("EXC", "EXCLUDES", constraint={"left_items": ["cpu"], "right_items": ["gpu"]}), True),
        "ONE_OF": (_rule("ONE", "ONE_OF", constraint={"items": ["CPU-1"]}), True),
        "MIN_QTY": (_rule("MIN", "MIN_QTY", constraint={"qty": "1"}), True),
        "MAX_QTY": (_rule("MAX", "MAX_QTY", constraint={"qty": "100"}), True),
        "CAPACITY": (_rule("CAP", "CAPACITY", constraint={"slot_code": "dimm"}), True),
        "ATTR_EQ": (_rule("AEQ", "ATTR_EQ", constraint={"attribute": "socket", "value": "AM5"}), True),
        "ATTR_RANGE": (
            _rule("ARGE", "ATTR_RANGE", constraint={"attribute": "watts", "min": "500", "max": "1000"}),
            True,
        ),
        "SUBSET_OF": (_rule("SUB", "SUBSET_OF", constraint={"items": ["cpu", "mb", "psu"]}), True),
    }
    for label, (rule, expected) in cases.items():
        passed, detail = compat.evaluate_rule(rule, components=components, attributes={}, slots=slots)
        assert passed is expected, (label, detail)
    # Capacity arithmetic: 2 DIMM slots used of 4 ok, then violate.
    over = _components() + [{"item_id": "ram1", "item_number": "RAM-1", "qty": "3", "slots": {"dimm": 1}}]
    passed, detail = compat.evaluate_rule(
        _rule("CAP2", "CAPACITY", constraint={"slot_code": "dimm"}), components=over, attributes={}, slots=slots
    )
    assert passed is False and detail["capacity"] == "4"
    assert Decimal(detail["used"]) == Decimal("6")  # 2+1 DIMM slots + 3 returned components


def test_configuration_hash_deterministic_and_order_independent() -> None:
    components = _components()
    first = compat.configuration_hash(
        assembly_item_id="asm-1",
        components=components,
        attributes={"socket": "AM5"},
        rule_set_versions={"B": 2, "A": 1},
    )
    reordered = list(reversed(components))
    second = compat.configuration_hash(
        assembly_item_id="asm-1", components=reordered, attributes={"socket": "AM5"}, rule_set_versions={"A": 1, "B": 2}
    )
    assert first == second and len(first) == 64
    third = compat.configuration_hash(
        assembly_item_id="asm-1",
        components=components,
        attributes={"socket": "LGA1700"},
        rule_set_versions={"A": 1, "B": 2},
    )
    assert third != first
