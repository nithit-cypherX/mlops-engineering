#!/usr/bin/env bash
# Host-side k6 commands; no cloud login, deployment, or model download.
set -euo pipefail
cd "$(dirname "$0")/.."
readonly K6_IMAGE='grafana/k6@sha256:9bd01d6941fca969cb61bb57d2da5ee9b385fe2aa8881df3798c196564d6ace6' # v2.2.0

mode=${1:-run}
script=k6.js
case "$mode" in
  check|cold-check)
    test_script=k6.test.js
    if [[ "$mode" == cold-check ]]; then test_script=cold-start.test.js; fi
    exec docker run --rm --pull=never --network none --read-only --cap-drop ALL \
      --security-opt no-new-privileges \
      --mount "type=bind,src=$PWD/loadtest,dst=/work/loadtest,readonly" \
      --mount "type=bind,src=$PWD/tests,dst=/work/tests,readonly" \
      "$K6_IMAGE" run --no-usage-report --include-system-env-vars=false "/work/tests/$test_script"
    ;;
  run) ;;
  cold) script=cold-start.js ;;
  *) printf 'Use run, check, cold or cold-check\n' >&2; exit 2 ;;
esac

: "${TARGET:?Set TARGET to the HTTPS endpoint followed by /predict}"
VUS=${VUS:-10}
DURATION=${DURATION:-60s}
if [[ "$mode" == cold ]]; then
  : "${ZERO_REPLICAS_CONFIRMED_AT:?Confirm zero replicas through Azure first; use its UTC check time}"
  VUS=1
  DURATION=60s # Not used by the cold script, which has one iteration and a fixed timeout.
fi
if [[ ! "$TARGET" =~ ^https://[a-zA-Z0-9.-]+(:[0-9]+)?/predict$ ]] \
  || [[ ! "$VUS" =~ ^([1-9]|[1-4][0-9]|50)$ ]] \
  || [[ ! "$DURATION" =~ ^[1-9][0-9]{0,2}s$ ]] || (( ${DURATION%s} > 300 )); then
  printf 'Use an HTTPS /predict URL, VUS=1..50 and DURATION=1s..300s. Baseline duration: 60s.\n' >&2
  exit 2
fi
docker image inspect "$K6_IMAGE" >/dev/null
mkdir -p reports/loadtest
prefix=run
if [[ "$mode" == cold ]]; then prefix=cold-start; fi
run_dir=$(mktemp -d "$PWD/reports/loadtest/$prefix-$(date -u +%Y%m%dT%H%M%SZ)-vus${VUS}-XXXXXX")
git_sha=$(git rev-parse HEAD)
git_dirty=false
if [[ -n "$(git status --porcelain -- . ':!reports/loadtest')" ]]; then git_dirty=true; fi
script_sha=$(sha256sum "loadtest/$script" | cut -d ' ' -f 1)
shared_script_sha=$(sha256sum loadtest/k6.js | cut -d ' ' -f 1)
if [[ "$mode" == cold ]]; then
  printf 'One first request only. Zero replicas must already be confirmed; do not warm up. Results: %s\n' "$run_dir"
else
  printf 'Warm round only. Readiness must already be confirmed. Results: %s\n' "$run_dir"
fi

set +e
docker run --rm --pull=never --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD/loadtest,dst=/work/loadtest,readonly" \
  --mount "type=bind,src=$run_dir,dst=/results" \
  "$K6_IMAGE" run --no-usage-report --include-system-env-vars=false \
  -e "TARGET=$TARGET" -e "VUS=$VUS" -e "DURATION=$DURATION" \
  -e "RUN_STARTED_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -e "GIT_SHA=$git_sha" -e "GIT_DIRTY=$git_dirty" \
  -e "SCRIPT_SHA256=$script_sha" -e "K6_IMAGE=$K6_IMAGE" \
  -e "SHARED_SCRIPT_SHA256=$shared_script_sha" \
  -e "ZERO_REPLICAS_CONFIRMED_AT=${ZERO_REPLICAS_CONFIRMED_AT:-}" \
  "/work/loadtest/$script" 2>&1 | tee "$run_dir/console.log"
statuses=("${PIPESTATUS[@]}")
set -e
printf '%s\n' "${statuses[0]}" > "$run_dir/exit-code.txt"
printf 'k6 exit: %s. Results kept in %s\n' "${statuses[0]}" "$run_dir"
if (( statuses[0] != 0 )); then exit "${statuses[0]}"; fi
exit "${statuses[1]}"
