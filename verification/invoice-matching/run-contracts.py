"""Regenerate this mainline port with task-local pinned dependencies, offline."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
evidence = root / "verification/invoice-matching"
base = "2dbdda9f0b4830c498733a599d12448c2b42309b"
assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip() == base
image = "sha256:4c2ec6485f4c72cdd4532e3a4186689ed9b0db8e17634a36d9be73bf2e38899e"
deps = root.parent / "invoice-scm-contract-deps"
code = '''import subprocess,sys,fastapi,pydantic
assert fastapi.__version__ == "0.138.0"
assert pydantic.__version__ == "2.13.4"
for args in [["tools/export_runtime_openapi.py"], ["app/contracts/procurement/build_from_app.py"], ["-m","app.contracts.cli","generate"], ["-m","app.contracts.cli","lint"]]:
 result=subprocess.run([sys.executable,*args],check=False)
 if result.returncode: raise SystemExit(result.returncode)
'''
args = ["docker", "run", "--rm", "--label", "codex.packet=IR-SCM-MAINLINE-CONTRACT-20261028", "--network", "none", "-v", str(root)+":/app", "-v", str(deps)+":/contract-deps:ro", "-e", "PYTHONPATH=/contract-deps:/app", "-e", "PYTHONDONTWRITEBYTECODE=1", image, "python", "-c", code]
result = subprocess.run(args, capture_output=True, text=True)
output = result.stdout + result.stderr
(evidence / "contracts-generate.log").write_text(output)
print(output[-4000:])
if result.returncode:
    raise SystemExit(result.returncode)
methods = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}

def operations(spec):
    return {f"{method.upper()} {path}" for path,item in spec["paths"].items() for method in item if method in methods}

report = {"base": base, "generator_versions": {"fastapi":"0.138.0", "pydantic":"2.13.4"}, "contract_lint": "passed", "web_types": "not applicable: mainline has no web project"}
for key,path in [("runtime","deployment/runtime.openapi.json"),("aggregate","contracts/openapi/openapi.json")]:
    previous = json.loads(subprocess.check_output(["git","show",f"{base}:{path}"],cwd=root,text=True))
    current = json.loads((root/path).read_text())
    oldops,newops = operations(previous),operations(current)
    removed,added = sorted(oldops-newops),sorted(newops-oldops)
    assert not removed and not added, (key,removed,added)
    changed_paths = sorted(p for p in set(previous["paths"])|set(current["paths"]) if previous["paths"].get(p)!=current["paths"].get(p))
    assert changed_paths == ["/api/v1/item-vendors","/api/v1/vendors"], (key,changed_paths)
    for path_name in changed_paths:
        assert {method for method in set(previous["paths"][path_name])|set(current["paths"][path_name]) if previous["paths"][path_name].get(method)!=current["paths"][path_name].get(method)} == {"get"}
    if key == "runtime":
        before = previous["components"]["schemas"]
        after = current["components"]["schemas"]
        changed_schemas = sorted(name for name in set(before)|set(after) if before.get(name)!=after.get(name))
        assert changed_schemas == ["ItemVendorPage","VendorPage"], changed_schemas
    report[key] = {"paths":len(current["paths"]), "operations":len(newops), "removed_operations":removed, "added_operations":added, "changed_get_paths":changed_paths, "sha256":hashlib.sha256((root/path).read_bytes()).hexdigest()}
allowed = {"app/api/routers/procurement/router.py","app/api/schemas/procurement.py","app/modules/procurement/service.py","app/contracts/procurement/openapi.fragment.json","contracts/openapi/fragments/FS-08.openapi.json","contracts/openapi/openapi.json","contracts/openapi/openapi.yaml","deployment/runtime.openapi.json"}
changed = set(subprocess.check_output(["git","diff","--name-only"],cwd=root,text=True).splitlines())
assert changed == allowed, sorted(changed ^ allowed)
report["tracked_files_changed"] = sorted(changed)
(evidence/"contracts.json").write_text(json.dumps(report,indent=2)+"\n")
print(json.dumps(report,indent=2))
