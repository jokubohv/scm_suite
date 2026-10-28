"""Offline pinned generation and complete operation-set comparison for P4."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
evidence = Path(__file__).resolve().parent
base = "2dbdda9f0b4830c498733a599d12448c2b42309b"
assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip() == base
image = "sha256:4c2ec6485f4c72cdd4532e3a4186689ed9b0db8e17634a36d9be73bf2e38899e"
deps = root.parent / "invoice-scm-contract-deps"
methods = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def operations(spec):
    return {f"{method.upper()} {path}" for path, item in spec["paths"].items() for method in item if method in methods}


before_path = evidence / "contracts-before.json"
if not before_path.exists():
    before = {}
    for name in ("deployment/runtime.openapi.json", "contracts/openapi/openapi.json"):
        spec = json.loads((root / name).read_text())
        before[name] = {"sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
                        "operations": sorted(operations(spec)), "paths": len(spec["paths"])}
    before_path.write_text(json.dumps(before, indent=2) + "\n")

code = '''import subprocess,sys,fastapi,pydantic
assert fastapi.__version__ == "0.138.0"
assert pydantic.__version__ == "2.13.4"
for args in [["tools/export_runtime_openapi.py"], ["app/contracts/procurement/build_from_app.py"], ["-m","app.contracts.cli","generate"], ["-m","app.contracts.cli","lint"]]:
 result=subprocess.run([sys.executable,*args],check=False)
 if result.returncode: raise SystemExit(result.returncode)
'''
args = ["docker", "run", "--rm", "--label", "codex.packet=COMPRAS-CONTRACT-20261028", "--network", "none",
        "-v", str(root) + ":/app", "-v", str(deps) + ":/contract-deps:ro",
        "-e", "PYTHONPATH=/contract-deps:/app", "-e", "PYTHONDONTWRITEBYTECODE=1", image, "python", "-c", code]
if "--verify-only" not in sys.argv:
    result = subprocess.run(args, capture_output=True, text=True)
    output = result.stdout + result.stderr
    (evidence / "contracts-generate.log").write_text(output)
    print(output[-1500:])
    if result.returncode:
        raise SystemExit(result.returncode)
report = {"base": base, "generator_versions": {"fastapi": "0.138.0", "pydantic": "2.13.4"}, "contract_lint": "passed"}
expected_paths = {"/api/v1/item-vendors", "/api/v1/vendors", "/api/v1/purchase-orders", "/api/v1/item-receipts", "/api/v1/vendor-bills"}
prior = json.loads(before_path.read_text())
for kind, name in (("runtime", "deployment/runtime.openapi.json"), ("aggregate", "contracts/openapi/openapi.json")):
    old = json.loads(subprocess.check_output(["git", "show", f"{base}:{name}"], cwd=root, text=True))
    current = json.loads((root / name).read_text())
    oldops, newops = operations(old), operations(current)
    assert oldops == newops == set(prior[name]["operations"]), "Unexpected operation-set change"
    changed = {path for path in old["paths"] if old["paths"][path] != current["paths"][path]}
    allowed_paths = expected_paths if kind == "runtime" else expected_paths | {"/api/v1/vendor-bills/{bill_id}", "/api/v1/vendor-bills/{id}"}
    assert changed == allowed_paths, sorted(changed)
    changes = {}
    for path in changed:
        changed_methods = {method for method in set(old["paths"][path]) | set(current["paths"][path])
                           if old["paths"][path].get(method) != current["paths"][path].get(method)}
        allowed_methods = {"get"}
        if kind == "aggregate" and path == "/api/v1/vendor-bills":
            allowed_methods = {"get", "post"}
        elif kind == "aggregate" and path.endswith(("/{id}", "/{bill_id}")):
            allowed_methods = {"get", "patch"}
        assert changed_methods == allowed_methods, (path, changed_methods)
        changes[path] = sorted(changed_methods)
        for method in changed_methods:
            left, right = old["paths"][path][method], current["paths"][path][method]
            keys = {key for key in set(left) | set(right) if left.get(key) != right.get(key)}
            assert keys <= {"parameters", "responses", "requestBody"}, (path, method, keys)
    data = {"paths": len(current["paths"]), "operations": len(newops), "added_operations": [], "removed_operations": [],
            "changed_path_methods": changes, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
    if kind == "runtime":
        before, after = old["components"]["schemas"], current["components"]["schemas"]
        data["changed_schemas"] = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        assert set(data["changed_schemas"]) == {"VendorPage", "ItemVendorPage", "VendorBillLineCreate", "VendorBillLineOut", "VendorBillOut", "VendorBillPage", "VendorBillMatchSummary", "PurchaseOrderPage", "ReceiptPage"}
        for key in data["changed_schemas"]:
            if key not in before:
                continue
            oldfields, newfields = before[key].get("properties", {}), after[key].get("properties", {})
            assert all(newfields.get(field) == spec for field, spec in oldfields.items()), (key, "existing field changed")
            assert before[key].get("required", []) == after[key].get("required", []), (key, "required fields changed")
    report[kind] = data
(evidence / "contracts.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
