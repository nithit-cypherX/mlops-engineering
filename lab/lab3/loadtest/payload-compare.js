// One warm user, four synthetic byte sizes. No readiness calls or retries.
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { validPrediction, setup as validateTarget, parseTiming } from './k6.js';
export { parseTiming } from './k6.js';
import { payloadCases } from './payload-cases.js';

export const rounds = 20;
export const tolerance = 1e-9;
const attempts = new Counter('payload_attempts');
const completed = new Counter('payload_completed');
const errors = new Counter('payload_errors');
const timeouts = new Counter('payload_timeouts');
const measured = new Counter('payload_measured');
const trends = {
  http_ms: new Trend('payload_http_ms', true),
  json_decode_ms: new Trend('payload_json_decode_ms', true),
  scoring_ms: new Trend('payload_scoring_ms', true),
  processing_ms: new Trend('payload_processing_ms', true),
  json_share: new Trend('payload_json_share'),
};
const thresholds = {
  http_reqs: ['count==84'],
  'http_reqs{phase:warmup}': ['count==4'],
  'http_reqs{phase:measured}': ['count==80'],
  payload_attempts: ['count==84'], payload_completed: ['count==84'],
  payload_errors: ['count==0'], payload_timeouts: ['count==0'],
  iterations: ['count==1'], checks: ['rate==1'],
};
for (const item of payloadCases) {
  thresholds['payload_measured{case:' + item.name + '}'] = ['count==20'];
  for (const name of Object.keys(trends)) {
    thresholds['payload_' + name + '{case:' + item.name + '}'] =
      name === 'json_share' ? ['min>=0', 'max<=1'] : [name === 'processing_ms' ? 'min>0' : 'min>=0'];
  }
}
export const options = {
  scenarios: { comparison: {
    executor: 'shared-iterations', vus: 1, iterations: 1,
    maxDuration: '180s', gracefulStop: '0s', // Hard stop; an interrupted comparison is invalid.
  } },
  maxRedirects: 0,
  summaryTrendStats: ['min', 'med', 'max', 'count'],
  thresholds,
};
export function setup() { validateTarget(); }

export function inspectResponse(res, reference = null) {
  const predictionValid = validPrediction(res);
  const probability = predictionValid ? res.json().probability : null;
  const timing = parseTiming(res.headers?.['Server-Timing']);
  const duration = res.timings?.duration;
  const httpMs = typeof duration === 'number' && Number.isFinite(duration) && duration >= 0 ? duration : null;
  const matches = predictionValid && (reference === null || Math.abs(probability - reference) <= tolerance);
  const reason = !predictionValid ? 'invalid-response'
    : !matches ? 'prediction-mismatch' : timing === null ? 'invalid-server-timing'
    : httpMs === null ? 'invalid-http-timing' : null;
  return { valid: reason === null, reason, status: res.status,
    error_code: res.error_code || null, timeout: res.error_code === 1050,
    probability, http_ms: httpMs, server: timing,
    server_timing_header: res.headers?.['Server-Timing'] || null,
    request_id: res.headers?.['X-Request-Id'] || null };
}

function send(item, phase, round, reference, post, observe) {
  attempts.add(1);
  const res = post(__ENV.TARGET, item.body, {
    headers: { 'Content-Type': 'application/json' }, timeout: '10s', redirects: 0,
    tags: { phase, case: item.name },
  });
  completed.add(1);
  const result = { phase, round, case: item.name, bytes: item.bytes, ...inspectResponse(res, reference) };
  errors.add(result.valid ? 0 : 1);
  timeouts.add(result.timeout ? 1 : 0);
  if (phase === 'measured') {
    const tags = { case: item.name };
    measured.add(1, tags);
    // Keep HTTP durations of failed requests too, but never fabricate server times.
    if (result.http_ms !== null) trends.http_ms.add(result.http_ms, tags);
    if (result.valid) {
      for (const name of Object.keys(result.server)) trends[name].add(result.server[name], tags);
    }
  }
  observe(result);
  return result;
}

export function runComparison(post = http.post, observe = () => {}) {
  let reference = null;
  for (const item of payloadCases) {
    const warm = send(item, 'warmup', 0, reference, post, observe);
    if (!warm.valid) return; // Do not measure an unready or incompatible endpoint.
    if (reference === null) reference = warm.probability;
  }
  for (let round = 0; round < rounds; round++) {
    for (let position = 0; position < payloadCases.length; position++) {
      const item = payloadCases[(round + position) % payloadCases.length];
      const result = send(item, 'measured', round + 1, reference, post, observe);
      if (!result.valid) return; // Keep the failed record, then stop without retry.
    }
  }
}

export default function () {
  runComparison(http.post, result => {
    check(result.valid, { 'valid prediction and separate timings': value => value });
    console.log(JSON.stringify({ kind: 'payload-comparison-request', ...result }));
  });
}

export function handleSummary(data) {
  const metrics = data.metrics || {};
  const count = name => {
    const value = metrics[name]?.values.count;
    return Number.isInteger(value) && value >= 0 ? value : null;
  };
  const cases = payloadCases.map(item => {
    const stats = {};
    for (const name of Object.keys(trends)) {
      stats[name] = metrics['payload_' + name + '{case:' + item.name + '}']?.values || null;
    }
    const complete = count('payload_measured{case:' + item.name + '}') === rounds
      && Object.values(stats).every(s => s?.count === rounds
        && [s.min, s.med, s.max].every(v => typeof v === 'number' && Number.isFinite(v) && v >= 0)
        && s.min <= s.med && s.med <= s.max)
      && stats.processing_ms.min > 0 && stats.json_share.max <= 1;
    return { case: item.name, bytes: item.bytes, measured_requests: count('payload_measured{case:' + item.name + '}'),
      complete, stats };
  });
  const valid = count('http_reqs') === 84 && count('http_reqs{phase:warmup}') === 4
    && count('http_reqs{phase:measured}') === 80 && count('iterations') === 1
    && count('payload_attempts') === 84 && count('payload_completed') === 84
    && count('payload_errors') === 0 && count('payload_timeouts') === 0
    && metrics.checks?.values.passes === 84 && metrics.checks?.values.fails === 0
    && cases.every(item => item.complete);
  const results = cases.map(item => ({
    case: item.case, bytes: item.bytes, measured_requests: item.measured_requests,
    timing_samples: item.stats.json_share?.count ?? 0, complete: item.complete,
    median_http_ms: valid ? item.stats.http_ms.med : null,
    median_json_decode_ms: valid ? item.stats.json_decode_ms.med : null,
    median_scoring_ms: valid ? item.stats.scoring_ms.med : null,
    median_processing_ms: valid ? item.stats.processing_ms.med : null,
    median_json_share: valid ? item.stats.json_share.med : null,
    json_majority: valid ? item.stats.json_share.med > 0.5 : null,
  }));
  const firstMajority = valid ? results.find(item => item.json_majority) : null;
  const result = {
    kind: 'warm-payload-comparison', target: __ENV.TARGET,
    started_utc: __ENV.RUN_STARTED_UTC || null,
    git_sha: __ENV.GIT_SHA || null, git_dirty: __ENV.GIT_DIRTY || null,
    script_sha256: __ENV.SCRIPT_SHA256 || null, shared_script_sha256: __ENV.SHARED_SCRIPT_SHA256 || null,
    payload_cases_sha256: __ENV.PAYLOAD_CASES_SHA256 || null, k6_image: __ENV.K6_IMAGE || null,
    model_version: '1', vus: 1, rounds, rows_per_request: 1,
    request_timeout: '10s', max_duration: '180s', graceful_stop: '0s', absolute_tolerance: tolerance,
    timing: 'HTTP duration separate from server timings; bodies prebuilt; warm-up excluded from medians',
    order: 'case index = (zero-based round + position) modulo 4',
    majority_rule: 'median of per-request json_decode / processing > 0.5; not share of HTTP time',
    requests: count('http_reqs'), warmup_requests: count('http_reqs{phase:warmup}'),
    measured_requests: count('http_reqs{phase:measured}'),
    attempts: count('payload_attempts'), completed_requests: count('payload_completed'),
    unfinished_requests: count('payload_attempts') !== null && count('payload_completed') !== null
      ? Math.max(0, count('payload_attempts') - count('payload_completed')) : null,
    error_count: count('payload_errors'), timeout_count: count('payload_timeouts'),
    valid_comparison: valid, cases: results,
    conclusion: !valid ? 'invalid-comparison' : firstMajority ? 'json-majority-observed' : 'not-observed-in-tested-range',
    first_tested_json_majority_bytes: firstMajority?.bytes ?? null,
  };
  return { '/results/summary.json': JSON.stringify({ lab3: result, k6: data }, null, 2) + '\n',
    stdout: JSON.stringify(result, null, 2) + '\n' };
}
