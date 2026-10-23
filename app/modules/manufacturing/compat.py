"""Δ-CFG compatibility engine (EXT/ASSUMPTION; binding addendum `design/CONFIG-REUSE-ADDENDUM.md`).

Pure, deterministic evaluation of dual-scope compatibility rules (no I/O):

* **Dual scope:** ``ASSEMBLY``/``BOM_REVISION`` baseline rules **and** context rules
  (``CONFIGURATION``/``ORDER``/``CUSTOMER``/``REGION``/``CHANNEL``) both apply with AND semantics.
* **Deterministic ordering:** scope specificity (order/configuration > customer > region/channel >
  assembly/BOM) → rule-set priority → rule-set code → rule code. Input order never changes the result.
* **Rule types:** REQUIRES, EXCLUDES, ONE_OF, MIN_QTY, MAX_QTY, CAPACITY, ATTR_EQ, ATTR_RANGE, SUBSET_OF.
* **Enforcement:** rule severity > rule-set ``enforcement_mode`` > tenant default (HARD). HARD produces a
  blocking result unless a valid override (scope + reason) stamps it OVERRIDDEN; WARN records a violation.
* **Evidence:** the caller persists an immutable ``compat_configuration`` row keyed by the deterministic
  ``configuration_hash`` + rule-set versions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.modules.manufacturing.errors import CompatibilityViolation

SCOPE_SPECIFICITY: dict[str, int] = {
    "ORDER": 10,
    "CONFIGURATION": 10,
    "CUSTOMER": 8,
    "REGION": 6,
    "CHANNEL": 6,
    "BOM_REVISION": 4,
    "ASSEMBLY": 3,
}
ENFORCEMENT_RANK: dict[str, int] = {"INHERIT": 0, "WARN": 1, "HARD": 2}
DEFAULT_ENFORCEMENT = "HARD"
RULE_TYPES = frozenset(
    {"REQUIRES", "EXCLUDES", "ONE_OF", "MIN_QTY", "MAX_QTY", "CAPACITY", "ATTR_EQ", "ATTR_RANGE", "SUBSET_OF"}
)


@dataclass(frozen=True, slots=True)
class RuleView:
    """Loaded compatibility rule (service maps a ``compat_rule`` row to this view)."""

    code: str
    rule_type: str
    subject_type: str
    subject_expr: Mapping[str, Any] = field(default_factory=dict)
    constraint_expr: Mapping[str, Any] = field(default_factory=dict)
    severity: str = "INHERIT"
    message_code: str = "COMPATIBILITY_VIOLATION"
    priority: int = 100
    effective_from: str | None = None
    effective_to: str | None = None
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class RuleSetView:
    code: str
    scope_type: str
    priority: int = 100
    enforcement_mode: str = "INHERIT"
    rules: tuple[RuleView, ...] = ()
    scope_ref_key: str | None = None
    status: str = "ACTIVE"


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    rule_set_code: str
    rule_code: str
    rule_type: str
    severity: str
    enforcement: str
    passed: bool
    message_code: str
    action: str
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    result: str
    outcomes: tuple[RuleOutcome, ...]
    matched_rule_sets: tuple[dict[str, Any], ...]
    matched_rules: tuple[dict[str, Any], ...]
    violation_count: int
    highest_enforcement: str

    @property
    def blocked(self) -> bool:
        return self.result == "HARD_FAIL"

    def as_payload(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "violations": [
                {
                    "rule_set_code": outcome.rule_set_code,
                    "rule_code": outcome.rule_code,
                    "rule_type": outcome.rule_type,
                    "severity": outcome.severity,
                    "enforcement": outcome.enforcement,
                    "message_code": outcome.message_code,
                    "detail": dict(outcome.detail),
                }
                for outcome in self.outcomes
                if not outcome.passed
            ],
            "outcomes": [
                {
                    "rule_set_code": outcome.rule_set_code,
                    "rule_code": outcome.rule_code,
                    "rule_type": outcome.rule_type,
                    "passed": outcome.passed,
                    "action": outcome.action,
                }
                for outcome in self.outcomes
            ],
            "matched_rule_sets": list(self.matched_rule_sets),
            "matched_rules": list(self.matched_rules),
            "violation_count": self.violation_count,
            "highest_enforcement": self.highest_enforcement,
        }


# --------------------------------------------------------------------------- helpers ----------
def _dec(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _component_map(components: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index selected components by item id and by item number (both accepted in expressions)."""
    index: dict[str, dict[str, Any]] = {}
    for component in components:
        for key in ("item_id", "item_number", "component_item_id", "component_item_number"):
            value = component.get(key)
            if value:
                index[str(value)] = dict(component)
    return index


def _subject_components(rule: RuleView, component_index: Mapping[str, Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    subject = rule.subject_expr or {}
    subject_type = str(rule.subject_type).upper()
    if subject_type == "ITEM":
        item = subject.get("item_id") or subject.get("item_number")
        return [component_index[str(item)]] if item and str(item) in component_index else []
    if subject_type == "COMPONENT_GROUP":
        group_items = {str(value) for value in subject.get("items", ()) or ()}
        return [component for key, component in component_index.items() if key in group_items]
    if subject_type == "ITEM_SET":
        allowed = {str(value) for value in subject.get("items", ()) or ()}
        return [component for key, component in component_index.items() if key in allowed]
    if subject_type == "ATTRIBUTE":
        attribute = subject.get("attribute")
        return [
            component
            for component in component_index.values()
            if attribute and (component.get("attributes") or {}).get(str(attribute)) is not None
        ]
    return []


def _quantity(component: Mapping[str, Any]) -> Decimal:
    for key in ("qty", "qty_selected", "quantity", "qty_per"):
        if component.get(key) is not None:
            return _dec(component.get(key))
    return Decimal("1")


def _attribute_value(component: Mapping[str, Any], attribute: str) -> Any:
    return (component.get("attributes") or {}).get(attribute)


def _attribute_ok(component: Mapping[str, Any], attribute: str, expected: Any) -> bool:
    actual = _attribute_value(component, attribute)
    if isinstance(expected, Mapping):
        if "eq" in expected:
            return actual == expected["eq"]
        if "in" in expected:
            return actual in expected["in"]
        if "min" in expected and _dec(actual) < _dec(expected["min"]):
            return False
        if "max" in expected and _dec(actual) > _dec(expected["max"]):
            return False
        return True
    return actual == expected


# --------------------------------------------------------------------------- engine -----------
def evaluate_rule(
    rule: RuleView,
    *,
    components: Sequence[Mapping[str, Any]],
    attributes: Mapping[str, Any],
    slots: Sequence[Mapping[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    """Evaluate one rule; returns ``(passed, detail)``. Rule-type semantics are fixed (RULE-MFG-27)."""
    rule_type = str(rule.rule_type).upper()
    if rule_type not in RULE_TYPES:
        return False, {"reason": "UNKNOWN_RULE_TYPE", "rule_type": rule_type}
    index = _component_map(components)
    subjects = _subject_components(rule, index)
    constraint = rule.constraint_expr or {}
    detail: dict[str, Any] = {"rule_type": rule_type, "subject_hits": len(subjects)}

    if rule_type == "REQUIRES":
        attribute = constraint.get("attribute")
        expected = constraint.get("value")
        candidates = subjects or [
            component
            for component in components
            if attribute is None or _attribute_value(component, str(attribute)) is not None
        ]
        if not candidates:
            candidates = list(components)
        passed = (
            any(_attribute_ok(component, str(attribute), expected) for component in candidates)
            if attribute is not None
            else bool(candidates)
        )
        return passed, detail
    if rule_type == "EXCLUDES":
        left = {str(value) for value in constraint.get("left_items", ()) or ()}
        right = {str(value) for value in constraint.get("right_items", ()) or ()}
        selected = set(index)
        passed = not (selected & left and selected & right)
        detail.update({"left_hits": len(selected & left), "right_hits": len(selected & right)})
        return passed, detail
    if rule_type == "ONE_OF":
        allowed = {str(value) for value in constraint.get("items", ()) or ()}
        hits = [key for key in index if key in allowed]
        passed = len(hits) == 1
        detail.update({"hits": hits})
        return passed, detail
    if rule_type in {"MIN_QTY", "MAX_QTY"}:
        target = constraint.get("item_id") or constraint.get("item_number")
        total = (
            sum((_quantity(component) for key, component in index.items() if key == str(target)), Decimal("0"))
            if target
            else sum((_quantity(component) for component in components), Decimal("0"))
        )
        threshold = _dec(constraint.get("qty") if "qty" in constraint else constraint.get("value"))
        passed = total >= threshold if rule_type == "MIN_QTY" else total <= threshold
        detail.update({"selected_qty": str(total), "threshold": str(threshold)})
        return passed, detail
    if rule_type == "CAPACITY":
        slot_code = str(constraint.get("slot_code") or (rule.subject_expr or {}).get("slot_code") or "")
        slot = next((item for item in slots if str(item.get("slot_code")) == slot_code), None)
        if slot is None:
            return False, {**detail, "reason": "SLOT_UNDEFINED", "slot_code": slot_code}
        capacity = _dec(slot.get("capacity"))
        used = Decimal("0")
        for component in components:
            component_slots = component.get("slots") or {}
            if slot_code in component_slots:
                used += _dec(component_slots[slot_code]) * _quantity(component)
            elif component.get("slot_code") == slot_code:
                used += _quantity(component) * _dec(slot.get("units_per_component", 1))
        passed = used <= capacity
        detail.update({"slot_code": slot_code, "used": str(used), "capacity": str(capacity)})
        return passed, detail
    if rule_type in {"ATTR_EQ", "ATTR_RANGE"}:
        attribute = str(constraint.get("attribute") or (rule.subject_expr or {}).get("attribute") or "")
        expected = constraint.get("value")
        if rule_type == "ATTR_RANGE":
            expected = {"min": constraint.get("min"), "max": constraint.get("max")}
        applicable = [
            component
            for component in (subjects or list(index.values()))
            if _attribute_value(component, attribute) is not None
        ]
        passed = bool(applicable) and all(_attribute_ok(component, attribute, expected) for component in applicable)
        detail.update({"attribute": attribute, "checked": len(applicable)})
        return passed, detail
    # SUBSET_OF
    allowed = {str(value) for value in constraint.get("items", ()) or ()}
    aliases = ("item_id", "item_number", "component_item_id", "component_item_number")
    unmatched = [
        component
        for component in components
        if not any(str(component.get(alias)) in allowed for alias in aliases if component.get(alias))
    ]
    passed = not unmatched
    detail.update(
        {
            "selected": sorted(
                {str(component.get("item_id") or component.get("item_number")) for component in components}
            )
        }
    )
    return passed, detail


def evaluate(
    rule_sets: Iterable[RuleSetView],
    *,
    components: Sequence[Mapping[str, Any]],
    attributes: Mapping[str, Any] | None = None,
    slots: Sequence[Mapping[str, Any]] = (),
    as_of: str | None = None,
    tenant_default: str = DEFAULT_ENFORCEMENT,
    override: bool = False,
) -> EvaluationResult:
    """Evaluate all matching ACTIVE rule sets deterministically (RULE-MFG-26/29/31, DM-MFG-12)."""
    as_of = as_of or "9999-12-31"
    matched_sets: list[RuleSetView] = []
    for rule_set in rule_sets:
        if str(rule_set.status).upper() != "ACTIVE":
            continue
        matched_sets.append(rule_set)
    matched_sets.sort(
        key=lambda item: (
            -SCOPE_SPECIFICITY.get(str(item.scope_type).upper(), 0),
            int(item.priority),
            item.code,
        )
    )
    outcomes: list[RuleOutcome] = []
    matched_rule_payload: list[dict[str, Any]] = []
    matched_set_payload: list[dict[str, Any]] = []
    for rule_set in matched_sets:
        matched_set_payload.append(
            {
                "code": rule_set.code,
                "scope_type": rule_set.scope_type,
                "priority": int(rule_set.priority),
                "enforcement_mode": rule_set.enforcement_mode,
                "scope_ref_key": rule_set.scope_ref_key,
            }
        )
        for rule in sorted(rule_set.rules, key=lambda item: (int(item.priority), item.code)):
            if not rule.is_active:
                continue
            if rule.effective_from and str(rule.effective_from) > as_of:
                continue
            if rule.effective_to and str(rule.effective_to) < as_of:
                continue
            passed, detail = evaluate_rule(rule, components=components, attributes=attributes or {}, slots=slots)
            severity = str(rule.severity).upper()
            rule_set_mode = str(rule_set.enforcement_mode).upper()
            enforcement = (
                severity
                if severity in {"HARD", "WARN"}
                else rule_set_mode
                if rule_set_mode in {"HARD", "WARN"}
                else str(tenant_default).upper()
            )
            if enforcement not in {"HARD", "WARN"}:
                enforcement = DEFAULT_ENFORCEMENT
            action = "PASSED" if passed else ("BLOCKED" if enforcement == "HARD" else "WARNED")
            outcomes.append(
                RuleOutcome(
                    rule_set_code=rule_set.code,
                    rule_code=rule.code,
                    rule_type=rule.rule_type,
                    severity=severity,
                    enforcement=enforcement,
                    passed=passed,
                    message_code=rule.message_code,
                    action=action,
                    detail=detail,
                )
            )
            matched_rule_payload.append(
                {
                    "rule_set_code": rule_set.code,
                    "rule_code": rule.code,
                    "rule_type": rule.rule_type,
                    "severity": severity,
                    "enforcement": enforcement,
                    "passed": passed,
                    "detail": detail,
                }
            )
    violations = [outcome for outcome in outcomes if not outcome.passed]
    hard = [outcome for outcome in violations if outcome.enforcement == "HARD"]
    warn = [outcome for outcome in violations if outcome.enforcement == "WARN"]
    if hard:
        result = "OVERRIDDEN" if override else "HARD_FAIL"
    elif warn:
        result = "WARN"
    else:
        result = "VALID"
    highest = "HARD" if hard else ("WARN" if warn else "INHERIT")
    return EvaluationResult(
        result=result,
        outcomes=tuple(outcomes),
        matched_rule_sets=tuple(matched_set_payload),
        matched_rules=tuple(matched_rule_payload),
        violation_count=len(violations),
        highest_enforcement=highest,
    )


def require_acceptable(result: EvaluationResult, *, override: bool = False) -> None:
    """Raise the typed HARD block unless a valid override was applied (DM-MFG-12)."""
    if result.blocked:
        raise CompatibilityViolation(
            "configuration violates a HARD compatibility rule set",
            details={"violations": result.as_payload()["violations"], "result": result.result},
        )
    if result.result == "OVERRIDDEN" and not override:  # pragma: no cover - defensive
        raise CompatibilityViolation("override requires an explicit override flag")


def configuration_hash(
    *,
    assembly_item_id: Any,
    components: Sequence[Mapping[str, Any]],
    attributes: Mapping[str, Any],
    rule_set_versions: Mapping[str, Any],
) -> str:
    ordered_components = [
        {
            "item": component.get("item_id") or component.get("item_number"),
            "qty": str(component.get("qty") or component.get("qty_selected") or ""),
            "slots": component.get("slots") or {},
        }
        for component in sorted(
            components,
            key=lambda item: (str(item.get("item_id") or item.get("item_number") or ""), str(item.get("qty") or "")),
        )
    ]
    ordered_attributes = {key: attributes[key] for key in sorted(attributes)}
    ordered_versions = {key: rule_set_versions[key] for key in sorted(rule_set_versions)}
    payload = {
        "assembly_item_id": str(assembly_item_id),
        "components": ordered_components,
        "attributes": ordered_attributes,
        "rule_set_versions": ordered_versions,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


__all__ = [
    "DEFAULT_ENFORCEMENT",
    "EvaluationResult",
    "RULE_TYPES",
    "RuleOutcome",
    "RuleSetView",
    "RuleView",
    "SCOPE_SPECIFICITY",
    "configuration_hash",
    "evaluate",
    "evaluate_rule",
    "require_acceptable",
]
