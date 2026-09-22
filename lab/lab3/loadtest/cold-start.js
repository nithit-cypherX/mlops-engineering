// One first prediction, not a warm load test. Confirm zero replicas externally first.
import http from 'k6/http';
import { check } from 'k6';
import { Trend, Gauge, Counter } from 'k6/metrics';
import { payload, validPrediction } from './k6.js';

const elapsed = new Trend('cold_client_elapsed_ms', true);
const status = new Gauge('cold_http_status');
const errorCode = new Gauge('cold_error_code');
const completed = new Counter('cold_completed');
export const requestTimeout = '120s';

export const options = {
  scenarios: { first_prediction: {
    executor: 'shared-iterations', vus: 1, iterations: 1,
    maxDuration: '130s', gracefulStop: '5s',
  } },
  maxRedirects: 0,
  summaryTrendStats: ['min', 'max', 'count'], // One observation, not p95/p99.
  thresholds: {
    http_reqs: ['count==1'], cold_completed: ['count==1'], checks: ['rate==1'],
  },
};

export function setup() {
  if (!/^(https:\/\/[a-zA-Z0-9.-]+(:[0-9]+)?|http:\/\/127\.0\.0\.1(:[0-9]+)?)\/predict$/.test(__ENV.TARGET || '')) {
    throw new Error('TARGET must be an HTTPS /predict URL (or loopback HTTP for a local check)');
  }
  // Operator-supplied metadata, not an Azure check. Loopback is only for local fixtures.
  if (__ENV.TARGET.startsWith('https:') &&
      (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(__ENV.ZERO_REPLICAS_CONFIRMED_AT || '') ||
       !Number.isFinite(Date.parse(__ENV.ZERO_REPLICAS_CONFIRMED_AT)))) {
    throw new Error('Confirm zero replicas through Azure, then set ZERO_REPLICAS_CONFIRMED_AT (UTC)');
  }
  // No readiness request, warm-up, or retry: the POST below must be the first request.
}

export default function () {
  const started = Date.now();
  const res = http.post(__ENV.TARGET, payload, {
    headers: { 'Content-Type': 'application/json' }, timeout: requestTimeout,
  });
  elapsed.add(Date.now() - started); // Includes DNS/connection/TLS in this client call.
  status.add(res.status);
  errorCode.add(res.error_code || 0);
  completed.add(1); // Includes failed responses and timeouts, not only successes.
  check(res, { 'valid prediction and model version': validPrediction });
}

export function handleSummary(data) {
  const metrics = data.metrics;
  const requests = metrics.http_reqs?.values.count || 0;
  const finished = metrics.cold_completed?.values.count || 0;
  const checks = metrics.checks?.values || {};
  const httpStatus = metrics.cold_http_status?.values.value ?? null;
  const code = metrics.cold_error_code?.values.value ?? null;
  const valid = requests === 1 && finished === 1 && checks.passes === 1 && checks.fails === 0;
  const result = {
    kind: 'cold-start-attempt', target: __ENV.TARGET,
    started_utc: __ENV.RUN_STARTED_UTC || null,
    zero_replicas_confirmed_at: __ENV.ZERO_REPLICAS_CONFIRMED_AT || null,
    git_sha: __ENV.GIT_SHA || null, git_dirty: __ENV.GIT_DIRTY || null,
    script_sha256: __ENV.SCRIPT_SHA256 || null,
    shared_script_sha256: __ENV.SHARED_SCRIPT_SHA256 || null,
    k6_image: __ENV.K6_IMAGE || null, model_version: '1',
    request_timeout: requestTimeout, requests, completed_requests: finished,
    http_status: httpStatus, error_code: code, timeout: code === 1050,
    valid_prediction: valid,
    outcome: finished !== 1 ? 'no_result' : code === 1050 ? 'timeout' :
      valid ? 'valid_prediction' : httpStatus > 0 ? 'invalid_response' : 'transport_error',
    client_elapsed_ms: metrics.cold_client_elapsed_ms?.values.min ?? null,
    http_duration_ms: metrics.http_req_duration?.values.min ?? null,
  };
  // Zero-replica evidence must be kept alongside this result before calling it a cold start.
  return {
    '/results/summary.json': JSON.stringify({ lab3: result, k6: data }, null, 2) + '\n',
    stdout: JSON.stringify(result, null, 2) + '\n',
  };
}
