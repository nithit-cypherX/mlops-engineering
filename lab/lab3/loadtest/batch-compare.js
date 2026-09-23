// Three paired warm comparisons. No deployment, readiness polling, or retries.
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Gauge, Trend } from 'k6/metrics';
import { payload, validPrediction, setup as validateTarget } from './k6.js';

export const rows = 100;
export const pairs = 3;
export const tolerance = 1e-9; // Same absolute tolerance as test_batch_matches_singles.
export const batchPayload = JSON.stringify({ rows: Array.from({ length: rows }, () => JSON.parse(payload)) });
const jobTime = new Trend('batch_compare_job_ms', true);
const pairValid = new Gauge('batch_compare_pair_valid');
const attempted = new Counter('batch_compare_attempts');
const completed = new Counter('batch_compare_completed');
const errors = new Counter('batch_compare_errors');
const timeouts = new Counter('batch_compare_timeouts');
const finishedPairs = new Counter('batch_compare_pairs');

const thresholds = {
  http_reqs: ['count==305'], // Two warm-ups + 3 * (100 singles + 1 batch).
  'http_reqs{phase:warmup}': ['count==2'],
  'http_reqs{phase:measured}': ['count==303'],
  batch_compare_attempts: ['count==305'],
  batch_compare_completed: ['count==305'],
  batch_compare_errors: ['count==0'],
  batch_compare_timeouts: ['count==0'],
  batch_compare_pairs: ['count==3'],
  checks: ['rate==1'],
};
for (let pair = 1; pair <= pairs; pair++) {
  // Tagged submetrics retain each pair in summary.json, not just an average.
  for (const mode of ['single', 'batch']) {
    thresholds[`batch_compare_job_ms{case:${mode}_${pair}}`] = ['min>0'];
  }
  thresholds[`batch_compare_pair_valid{pair:${pair}}`] = ['value==1'];
}
export const options = {
  scenarios: { comparison: {
    executor: 'shared-iterations', vus: 1, iterations: 1,
    maxDuration: '180s', gracefulStop: '15s',
  } },
  maxRedirects: 0,
  summaryTrendStats: ['min', 'med', 'max', 'count'], // Three pairs do not establish tail latency.
  thresholds,
};

export function setup() { validateTarget(); }

export function validBatch(res) {
  if (res.status !== 200) return false;
  let body;
  try { body = res.json(); } catch (_) { return false; }
  return body !== null && typeof body === 'object'
    && body.model_version === '1' && res.headers['X-Model-Version'] === '1'
    && Array.isArray(body.probabilities) && body.probabilities.length === rows
    && body.probabilities.every(p => typeof p === 'number' && Number.isFinite(p) && p >= 0 && p <= 1);
}

function send(mode, phase, post) {
  attempted.add(1);
  const res = post(__ENV.TARGET + (mode === 'batch' ? '/batch' : ''), mode === 'batch' ? batchPayload : payload, {
    headers: { 'Content-Type': 'application/json' }, timeout: '10s', redirects: 0,
    tags: { phase, mode },
  });
  completed.add(1);
  const valid = mode === 'batch' ? validBatch(res) : validPrediction(res);
  errors.add(valid ? 0 : 1);
  timeouts.add(res.error_code === 1050 ? 1 : 0);
  return { res, valid };
}

// Injected post/clock let the existing offline k6 tests check counts and timing without HTTP.
export function measure(mode, post = http.post, clock = Date.now) {
  const scores = [];
  let failures = 0, timeoutCount = 0;
  const started = clock();
  for (let i = 0; i < (mode === 'single' ? rows : 1); i++) {
    const { res, valid } = send(mode, 'measured', post);
    failures += valid ? 0 : 1;
    timeoutCount += res.error_code === 1050 ? 1 : 0;
    if (valid) scores.push(...(mode === 'batch' ? res.json().probabilities : [res.json().probability]));
  }
  return { elapsed_ms: clock() - started, requests: mode === 'single' ? rows : 1,
    valid_rows: scores.length, errors: failures, timeouts: timeoutCount, scores };
}

export function runPair(pair, post = http.post, clock = Date.now) {
  const order = pair % 2 === 1 ? ['single', 'batch'] : ['batch', 'single'];
  const result = {};
  for (const mode of order) result[mode] = measure(mode, post, clock);
  const single = result.single, batch = result.batch;
  const equivalent = single.errors === 0 && batch.errors === 0
    && single.scores.length === rows && batch.scores.length === rows
    && single.scores.every((p, i) => Math.abs(p - batch.scores[i]) <= tolerance);
  delete single.scores;
  delete batch.scores;
  return { pair, order, single, batch, equivalent,
    speedup: equivalent && single.elapsed_ms > 0 && batch.elapsed_ms > 0
      ? single.elapsed_ms / batch.elapsed_ms : null };
}

export default function () {
  // Same VU/connection as the measured work; neither request enters a job timer.
  const singleWarm = send('single', 'warmup', http.post);
  const batchWarm = send('batch', 'warmup', http.post);
  check(singleWarm.valid, { 'single warm-up valid': value => value });
  check(batchWarm.valid, { 'batch warm-up valid': value => value });
  if (!singleWarm.valid || !batchWarm.valid) return;

  for (let pair = 1; pair <= pairs; pair++) {
    const result = runPair(pair);
    for (const mode of ['single', 'batch']) jobTime.add(result[mode].elapsed_ms, { case: `${mode}_${pair}` });
    pairValid.add(result.equivalent ? 1 : 0, { pair: String(pair) });
    finishedPairs.add(1);
    check(result.equivalent, { '100 batch predictions match 100 singles': value => value });
    console.log(JSON.stringify({ kind: 'batch-comparison-pair', ...result }));
  }
}

export function handleSummary(data) {
  const metrics = data.metrics || {};
  const count = name => metrics[name]?.values.count || 0;
  const results = [];
  for (let pair = 1; pair <= pairs; pair++) {
    const single = metrics[`batch_compare_job_ms{case:single_${pair}}`]?.values;
    const batch = metrics[`batch_compare_job_ms{case:batch_${pair}}`]?.values;
    const equivalent = metrics[`batch_compare_pair_valid{pair:${pair}}`]?.values.value === 1;
    const singleMs = single?.count === 1 ? single.min : null;
    const batchMs = batch?.count === 1 ? batch.min : null;
    results.push({ pair, order: pair % 2 === 1 ? ['single', 'batch'] : ['batch', 'single'],
      single_ms: singleMs, batch_ms: batchMs, equivalent,
      speedup: equivalent && singleMs > 0 && batchMs > 0 ? singleMs / batchMs : null });
  }
  const valid = count('batch_compare_pairs') === pairs && count('http_reqs') === 305
    && count('http_reqs{phase:warmup}') === 2 && count('http_reqs{phase:measured}') === 303
    && count('batch_compare_attempts') === 305 && count('batch_compare_completed') === 305
    && count('batch_compare_errors') === 0 && count('batch_compare_timeouts') === 0
    && metrics.checks?.values.passes === 5 && metrics.checks?.values.fails === 0
    && results.every(p => p.speedup !== null);
  const medianSingle = valid ? results.map(p => p.single_ms).sort((a, b) => a - b)[1] : null;
  const medianBatch = valid ? results.map(p => p.batch_ms).sort((a, b) => a - b)[1] : null;
  const result = {
    kind: 'warm-batch-comparison', target: __ENV.TARGET,
    started_utc: __ENV.RUN_STARTED_UTC || null,
    git_sha: __ENV.GIT_SHA || null, git_dirty: __ENV.GIT_DIRTY || null,
    script_sha256: __ENV.SCRIPT_SHA256 || null, shared_script_sha256: __ENV.SHARED_SCRIPT_SHA256 || null,
    k6_image: __ENV.K6_IMAGE || null, model_version: '1', vus: 1, rows_per_job: rows,
    request_timeout: '10s', max_duration: '180s', graceful_stop: '15s', absolute_tolerance: tolerance,
    timing: 'client elapsed time for all 100 rows, including response validation; payloads prebuilt; warm-up excluded',
    requests: count('http_reqs'), warmup_requests: count('http_reqs{phase:warmup}'),
    measured_requests: count('http_reqs{phase:measured}'),
    attempts: count('batch_compare_attempts'), completed_requests: count('batch_compare_completed'),
    unfinished_requests: Math.max(0, count('batch_compare_attempts') - count('batch_compare_completed')),
    error_count: count('batch_compare_errors'), timeout_count: count('batch_compare_timeouts'),
    completed_pairs: count('batch_compare_pairs'), pairs: results, valid_comparison: valid,
    median_single_ms: medianSingle, median_batch_ms: medianBatch,
    speedup_ratio_of_medians: valid ? medianSingle / medianBatch : null,
  };
  return {
    '/results/summary.json': JSON.stringify({ lab3: result, k6: data }, null, 2) + '\n',
    stdout: JSON.stringify(result, null, 2) + '\n',
  };
}
