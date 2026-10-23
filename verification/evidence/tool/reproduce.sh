#!/usr/bin/env bash
# Reproduces all Phase-A tool evidence under verification/evidence/tool/.
# Run from inventory-platform/:  bash verification/evidence/tool/reproduce.sh
set -u
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
EV="verification/evidence/tool"
mkdir -p "$EV/check-env" "$EV/record" "$EV/adversarial"

echo "== tool help =="
python3 tools/feature_sets.py --help > "$EV/help-main.log" 2>&1; echo "help rc=$?"
python3 tools/feature_sets.py check-env --help > "$EV/help-check-env.log" 2>&1
python3 tools/feature_sets.py record --help > "$EV/help-record.log" 2>&1

echo "== L0 validate =="
{ python3 tools/feature_sets.py validate; echo "rc=$?"; } > "$EV/validate.log" 2>&1

echo "== order =="
{ python3 tools/feature_sets.py order --phase 1; echo "rc=$?"; } > "$EV/order-phase1.log" 2>&1
{ python3 tools/feature_sets.py order --phase 1 --json; echo "rc=$?"; } > "$EV/order-phase1.json" 2>&1

echo "== coverage =="
{ python3 tools/feature_sets.py coverage; echo "rc=$?"; } > "$EV/coverage.log" 2>&1

echo "== deploy-order =="
{ python3 tools/feature_sets.py deploy-order --phase 1; echo "rc=$?"; } > "$EV/deploy-order-phase1.log" 2>&1

echo "== consumer impact =="
{ python3 tools/feature_sets.py who-depends-on --contract inventory.ledger; echo "rc=$?"; } > "$EV/who-depends-on-inventory-ledger.log" 2>&1
{ python3 tools/feature_sets.py who-depends-on --contract platform.core; echo "rc=$?"; } > "$EV/who-depends-on-platform-core.log" 2>&1

echo "== unit tests =="
{ python3 -m unittest verification/tests/test_feature_sets.py -v; echo "rc=$?"; } > "$EV/unit-tests.log" 2>&1

echo "== check-env gate runs =="
{ python3 tools/feature_sets.py check-env --set FS-00 --env dev --state "$EV/state/dev-greenfield.json"; echo "rc=$?"; } > "$EV/check-env/fs00-dev-greenfield.log" 2>&1
{ python3 tools/feature_sets.py check-env --set FS-01 --env dev --state "$EV/state/dev-wave0.json"; echo "rc=$?"; } > "$EV/check-env/fs01-dev-wave0-pass.log" 2>&1
{ python3 tools/feature_sets.py check-env --set FS-04 --env dev --state "$EV/state/dev-wave0.json"; echo "rc=$?"; } > "$EV/check-env/fs04-dev-wave0-missing-fs03.log" 2>&1
{ python3 tools/feature_sets.py check-env --set FS-01 --env staging --state "$EV/state/staging-tampered.json"; echo "rc=$?"; } > "$EV/check-env/fs01-staging-tampered.log" 2>&1
{ python3 tools/feature_sets.py check-env --set FS-03 --env staging --state "$EV/state/staging-tampered.json"; echo "rc=$?"; } > "$EV/check-env/fs03-staging-tampered.log" 2>&1

echo "== record flows =="
cat > "$EV/record/FS-00-exit-checks.log" <<'LOG'
[fixture evidence] alembic upgrade head: OK
[fixture evidence] pytest app/tests/integration/platform -q: 42 passed
[fixture evidence] pytest app/tests/invariants/test_rls_tenant_isolation.py: 3 passed
[fixture evidence] record --status verified: allowed
LOG
rm -f "$EV/record/dev-state-after-record.json"
{ python3 tools/feature_sets.py record --set FS-00 --env dev --status verified \
    --evidence "$EV/record/FS-00-exit-checks.log" --state "$EV/record/dev-state-after-record.json"; echo "rc=$?"; } \
  > "$EV/record/fs00-verified.log" 2>&1
{ python3 tools/feature_sets.py record --set FS-01 --env dev --status verified \
    --evidence "$EV/record/FS-00-exit-checks.log" --state "$EV/record/dev-state-after-record.json"; echo "rc=$?"; } \
  > "$EV/record/fs01-verified-after-dep.log" 2>&1
cp "$EV/record/dev-state-after-record.json" "$EV/record/dev-state-snapshot.json"
rm -f "$EV/record/dev-state-refusals.json"
{ python3 tools/feature_sets.py record --set FS-00 --env dev --status verified \
    --state "$EV/record/dev-state-refusals.json"; echo "rc=$?"; } \
  > "$EV/record/record-refused-no-evidence.log" 2>&1
{ python3 tools/feature_sets.py record --set FS-01 --env dev --status verified \
    --evidence "$EV/record/FS-00-exit-checks.log" --state "$EV/record/dev-state-refusals.json"; echo "rc=$?"; } \
  > "$EV/record/record-refused-unverified-dep.log" 2>&1
{ python3 tools/feature_sets.py record --set FS-00 --env dev --status verified \
    --evidence "$EV/record/does-not-exist.log" --state "$EV/record/dev-state-refusals.json"; echo "rc=$?"; } \
  > "$EV/record/record-refused-missing-evidence-file.log" 2>&1

echo "== adversarial manifest rejections =="
for fixture in verification/fixtures/manifests/*.json; do
  name="$(basename "$fixture" .json)"
  { python3 tools/feature_sets.py validate --manifest "$fixture"; echo "rc=$?"; } \
    > "$EV/adversarial/validate-$name.log" 2>&1
done

echo "== live /internal/deps mode smoke =="
mkdir -p "$EV/live"
python3 - <<'PY2'
import json
deps={"environment":"dev","feature_sets":{
 "FS-00":{"status":"verified","contracts":{"platform.core":"1.0.0","platform.events":"1.0.0","platform.finance":"1.0.0","order.demand":"0.1.0","order.supply":"0.1.0"},"health":"ok"},
 "FS-01":{"status":"verified","contracts":{"item.master":"1.0.0"},"health":"ok"}}}
open('verification/evidence/tool/live/internal-deps.json','w').write(json.dumps(deps,indent=2)+"\n")
open('verification/evidence/tool/live/internal-deps-malformed.json','w').write('{"unexpected":"shape"}')
PY2
{ python3 tools/feature_sets.py check-env --set FS-02 --env dev --deps-url "file://$ROOT/verification/evidence/tool/live/internal-deps.json"; echo "rc=$?"; } \
  > "$EV/live/check-env-live-pass.log" 2>&1
{ python3 tools/feature_sets.py check-env --set FS-02 --env dev --deps-url "file://$ROOT/verification/evidence/tool/live/internal-deps-malformed.json"; echo "rc=$?"; } \
  > "$EV/live/check-env-live-malformed.log" 2>&1

echo "== done =="
ls "$EV"

echo "== environment claims =="
{ echo "python: $(python3 --version)"; echo "docker: $(docker --version)"; echo "compose: $(docker compose version)"; echo "git: $(git --version)"; } \
  > "$EV/environment.log" 2>&1
{ python3 tools/render_plan_tables.py --check; echo "rc=$?"; } > "$EV/render-plan-tables-check.log" 2>&1
echo "== live /internal/deps mode smoke =="
mkdir -p "$EV/live"
python3 - <<'PY2'
import json
deps={"environment":"dev","feature_sets":{
 "FS-00":{"status":"verified","contracts":{"platform.core":"1.0.0","platform.events":"1.0.0","platform.finance":"1.0.0","order.demand":"0.1.0","order.supply":"0.1.0"},"health":"ok"},
 "FS-01":{"status":"verified","contracts":{"item.master":"1.0.0"},"health":"ok"}}}
open('verification/evidence/tool/live/internal-deps.json','w').write(json.dumps(deps,indent=2)+"\n")
open('verification/evidence/tool/live/internal-deps-malformed.json','w').write('{"unexpected":"shape"}')
PY2
{ python3 tools/feature_sets.py check-env --set FS-02 --env dev --deps-url "file://$ROOT/verification/evidence/tool/live/internal-deps.json"; echo "rc=$?"; } \
  > "$EV/live/check-env-live-pass.log" 2>&1
{ python3 tools/feature_sets.py check-env --set FS-02 --env dev --deps-url "file://$ROOT/verification/evidence/tool/live/internal-deps-malformed.json"; echo "rc=$?"; } \
  > "$EV/live/check-env-live-malformed.log" 2>&1

echo "== done =="
