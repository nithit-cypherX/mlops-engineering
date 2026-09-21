// Adapted from pasdptt/public_teaching_mlaiops @ 317f56ebf8563211d338c372c0db8d3a09e2004d.
// Warm single-prediction rounds only. Cold start and variable comparisons are separate.
import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';

const latency = new Trend('predict_latency_ms', true);
const failures = new Rate('predict_failures');
const attempts = new Counter('predict_attempts');
const timeouts = new Counter('predict_timeouts');
const vus = Number(__ENV.VUS || 10);
const duration = __ENV.DURATION || '60s';

export const options = {
  vus, duration,
  gracefulStop: '15s', // Longer than the 10s warm-request timeout.
  maxRedirects: 0,
  summaryTrendStats: ['min', 'med', 'p(95)', 'p(99)', 'max', 'count'],
  summaryTimeUnit: 'ms',
  thresholds: {
    predict_latency_ms: ['p(95)<200'], // Agreed in Task 3.1, before load testing.
    predict_failures: ['rate==0'],
    predict_attempts: ['count>0'],
  },
};

export const payload = JSON.stringify({
  temp_c: 78.4, vibration_mm_s: 3.1, pressure_kpa: 315.2,
  hours_since_service: 4200, load_pct: 68.0, ambient_humidity: 55.0,
});

export function setup() {
  // HTTPS for real endpoints; loopback HTTP is only for isolated fixture checks.
  if (!/^(https:\/\/[a-zA-Z0-9.-]+(:[0-9]+)?|http:\/\/127\.0\.0\.1(:[0-9]+)?)\/predict$/.test(__ENV.TARGET || '')) {
    throw new Error('TARGET must be an HTTPS /predict URL (or loopback HTTP for a local check)');
  }
  if (!Number.isInteger(vus) || vus < 1 || vus > 50 || !/^[1-9][0-9]*s$/.test(duration) || parseInt(duration) > 300) {
    throw new Error('Use VUS=1..50 and DURATION=1s..300s; the baseline is 60s');
  }
  // Readiness is confirmed outside this script, so it cannot warm a cold-start test.
}

export function validPrediction(res) {
  if (res.status !== 200) return false;
  let body;
  try { body = res.json(); } catch (_) { return false; }
  return body !== null && typeof body === 'object'
    && typeof body.probability === 'number' && Number.isFinite(body.probability)
    && body.probability >= 0 && body.probability <= 1
    && body.model_version === '1' && res.headers['X-Model-Version'] === '1';
}

export default function () {
  attempts.add(1);
  const res = http.post(__ENV.TARGET, payload, {
    headers: { 'Content-Type': 'application/json' }, timeout: '10s',
  });
  latency.add(res.timings.duration); // Include failed and slow responses too.
  timeouts.add(res.error_code === 1050 ? 1 : 0);
  const valid = check(res, { 'valid prediction and model version': validPrediction });
  failures.add(!valid);
}

export function handleSummary(data) {
  const timing = data.metrics.predict_latency_ms?.values || {};
  const failed = data.metrics.predict_failures?.values || {};
  const started = data.metrics.predict_attempts?.values.count || 0;
  const errors = failed.passes || 0; // Rate samples are true when a prediction fails.
  const completed = errors + (failed.fails || 0);
  const elapsed = data.state.testRunDurationMs / 1000;
  const unfinished = Math.max(0, started - completed);
  const result = {
    kind: 'warm-single-prediction', target: __ENV.TARGET,
    started_utc: __ENV.RUN_STARTED_UTC || null,
    git_sha: __ENV.GIT_SHA || null, git_dirty: __ENV.GIT_DIRTY || null,
    script_sha256: __ENV.SCRIPT_SHA256 || null, k6_image: __ENV.K6_IMAGE || null,
    model_version: '1', vus, configured_duration: duration,
    elapsed_seconds: elapsed, attempts: started, completed_responses: completed,
    unfinished_requests: unfinished, error_count: errors,
    timeout_count: data.metrics.predict_timeouts?.values.count || 0,
    error_rate: completed ? errors / completed : null,
    requests_per_second: elapsed > 0 ? completed / elapsed : null,
    p50_ms: timing.med ?? null, p95_ms: timing['p(95)'] ?? null,
    p99_ms: timing['p(99)'] ?? null,
    meets_target: completed > 0 && unfinished === 0 && errors === 0
      && Number.isFinite(timing['p(95)']) && timing['p(95)'] < 200,
  };
  // Keep native metrics/threshold outcomes as well as the compact lab summary.
  return {
    '/results/summary.json': JSON.stringify({ lab3: result, k6: data }, null, 2) + '\n',
    stdout: JSON.stringify(result, null, 2) + '\n',
  };
}
