// Offline checks in the pinned k6 runtime. All HTTP/clock calls below are stubs.
import { check } from 'k6';
import { payload } from '../loadtest/k6.js';
import { options as comparisonOptions, batchPayload, validBatch, measure, runPair, handleSummary } from '../loadtest/batch-compare.js';

export const options = { vus: 1, iterations: 1, thresholds: { checks: ['rate==1'] } };
function verify(name, condition) { check(condition, { [name]: value => value === true }); }
function response(probabilities, status = 200, version = '1', header = '1') {
  return { status, headers: { 'X-Model-Version': header }, json: () => ({ probabilities, model_version: version }) };
}

export default function () {
  const scores = Array(100).fill(0.5);
  verify('valid batch', validBatch(response(scores)));
  verify('probability boundaries accepted', validBatch(response(Array.from({ length: 100 }, (_, i) => i % 2))));
  for (const size of [0, 1, 99, 101]) verify(`reject ${size} rows`, !validBatch(response(Array(size).fill(0.5))));
  for (const value of [-0.1, 1.1, NaN, Infinity, true, '0.5', null]) {
    verify(`reject invalid probability ${String(value)}`, !validBatch(response([value, ...scores.slice(1)])));
  }
  verify('reject wrong status', !validBatch(response(scores, 503)));
  verify('reject wrong model', !validBatch(response(scores, 200, '2')));
  verify('reject wrong header', !validBatch(response(scores, 200, '1', '2')));
  verify('reject missing probabilities', !validBatch(response(undefined)));
  verify('reject null body', !validBatch({ status: 200, json: () => null }));
  verify('reject malformed JSON', !validBatch({ status: 200, json() { throw new Error('bad JSON'); } }));
  const preparedRows = JSON.parse(batchPayload).rows;
  verify('same payload exactly 100 times', preparedRows.length === 100 && preparedRows.every(r => JSON.stringify(r) === payload));
  verify('one user one iteration with bounded runtime', comparisonOptions.scenarios.comparison.vus === 1
    && comparisonOptions.scenarios.comparison.iterations === 1 && comparisonOptions.scenarios.comparison.maxDuration === '180s');
  verify('redirects disabled', comparisonOptions.maxRedirects === 0);
  verify('warm-up and measured counts separated', comparisonOptions.thresholds['http_reqs{phase:warmup}'][0] === 'count==2'
    && comparisonOptions.thresholds['http_reqs{phase:measured}'][0] === 'count==303');
  verify('not a 200ms or batch-must-win threshold', !Object.values(comparisonOptions.thresholds).flat().some(s => s.includes('200')));

  let calls = [], time = 0, failure = null;
  const post = (url, body, params) => {
    const batch = url.endsWith('/batch');
    calls.push({ batch, body, params });
    time += batch ? 20 : 10;
    if (failure === 'timeout') return { status: 0, error_code: 1050 };
    if (failure === 'invalid') return response([], 503);
    if (batch) return response(Array(100).fill(failure === 'mismatch' ? 0.6 : failure === 'within-tolerance' ? 0.5000000005 : 0.5));
    return { status: 200, headers: { 'X-Model-Version': '1' }, json: () => ({ probability: 0.5, model_version: '1' }) };
  };
  const clock = () => time;
  const single = measure('single', post, clock);
  verify('100 sequential single calls counted and timed', calls.length === 100 && single.requests === 100
    && single.elapsed_ms === 1000 && single.valid_rows === 100 && single.errors === 0);
  verify('single requests use original payload and timeout', calls.every(c => c.body === payload
    && c.params.timeout === '10s' && c.params.redirects === 0 && c.params.tags.phase === 'measured'));
  calls = [];
  const batch = measure('batch', post, clock);
  verify('one batch call has the same 100 rows', calls.length === 1 && calls[0].body === batchPayload
    && batch.requests === 1 && batch.elapsed_ms === 20 && batch.valid_rows === 100);
  for (const pair of [1, 2, 3]) {
    calls = [];
    const result = runPair(pair, post, clock);
    verify(`pair ${pair}: exact count and alternating order`, calls.length === 101 && calls[0].batch === (pair === 2)
      && calls.filter(c => c.batch).length === 1 && result.order[0] === (pair === 2 ? 'batch' : 'single'));
    verify(`pair ${pair}: equality and ratio use total job time`, result.equivalent && result.speedup === 50);
  }
  failure = 'within-tolerance';
  verify('existing absolute tolerance accepted', runPair(1, post, clock).equivalent);
  failure = 'mismatch';
  const mismatch = runPair(1, post, clock);
  verify('different predictions cannot claim speedup', !mismatch.equivalent && mismatch.speedup === null);
  failure = 'invalid';
  calls = [];
  const invalid = runPair(1, post, clock);
  verify('errors retained without retry or speedup', calls.length === 101 && invalid.single.errors === 100
    && invalid.batch.errors === 1 && !invalid.equivalent && invalid.speedup === null);
  failure = 'timeout';
  calls = [];
  const timedOut = measure('batch', post, clock);
  verify('timeout counted without retry', calls.length === 1 && timedOut.errors === 1 && timedOut.timeouts === 1 && timedOut.valid_rows === 0);

  const metrics = {
    http_reqs: { values: { count: 305 } }, 'http_reqs{phase:warmup}': { values: { count: 2 } },
    'http_reqs{phase:measured}': { values: { count: 303 } },
    batch_compare_attempts: { values: { count: 305 } }, batch_compare_completed: { values: { count: 305 } },
    batch_compare_errors: { values: { count: 0 } }, batch_compare_timeouts: { values: { count: 0 } },
    batch_compare_pairs: { values: { count: 3 } }, checks: { values: { passes: 5, fails: 0 } },
  };
  for (let pair = 1; pair <= 3; pair++) {
    metrics[`batch_compare_job_ms{case:single_${pair}}`] = { values: { count: 1, min: pair * 1000 } };
    metrics[`batch_compare_job_ms{case:batch_${pair}}`] = { values: { count: 1, min: pair * 20 } };
    metrics[`batch_compare_pair_valid{pair:${pair}}`] = { values: { value: 1 } };
  }
  const summarize = data => JSON.parse(handleSummary(data)['/results/summary.json']);
  const saved = summarize({ metrics });
  verify('three raw pairs and correct medians', saved.lab3.pairs.length === 3 && saved.lab3.median_single_ms === 2000
    && saved.lab3.median_batch_ms === 40 && saved.lab3.speedup_ratio_of_medians === 50);
  verify('summary preserves native metrics and warm-up separation', saved.k6.metrics.http_reqs.values.count === 305
    && saved.lab3.warmup_requests === 2 && saved.lab3.measured_requests === 303 && saved.lab3.valid_comparison);
  for (const metric of ['batch_compare_errors', 'batch_compare_timeouts']) {
    const broken = JSON.parse(JSON.stringify({ metrics }));
    broken.metrics[metric].values.count = 1;
    const result = summarize(broken).lab3;
    verify(`${metric}: no aggregate speedup`, !result.valid_comparison && result.speedup_ratio_of_medians === null);
  }
  const interrupted = JSON.parse(JSON.stringify({ metrics }));
  interrupted.metrics.batch_compare_completed.values.count = 304;
  const unfinished = summarize(interrupted).lab3;
  verify('unfinished request prevents valid comparison', unfinished.unfinished_requests === 1 && !unfinished.valid_comparison);
  const missing = JSON.parse(JSON.stringify({ metrics }));
  delete missing.metrics['batch_compare_job_ms{case:batch_3}'];
  verify('missing pair time prevents success', !summarize(missing).lab3.valid_comparison);
  const zeroTime = JSON.parse(JSON.stringify({ metrics }));
  zeroTime.metrics['batch_compare_job_ms{case:batch_1}'].values.min = 0;
  verify('zero-duration sample cannot produce speedup', !summarize(zeroTime).lab3.valid_comparison);
  const empty = summarize({ metrics: {} }).lab3;
  verify('empty results never pass', !empty.valid_comparison && empty.speedup_ratio_of_medians === null);
}
