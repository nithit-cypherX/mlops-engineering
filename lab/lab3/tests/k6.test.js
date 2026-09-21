// Offline checks in the real k6 runtime. No HTTP calls, model, or credentials.
import { check } from 'k6';
import { validPrediction, handleSummary, options as loadOptions, payload } from '../loadtest/k6.js';

export const options = { vus: 1, iterations: 1, thresholds: { checks: ['rate==1'] } };

function verify(name, condition) {
  check(condition, { [name]: value => value === true });
}

function response(body, status = 200, version = '1') {
  return { status, headers: { 'X-Model-Version': version }, json: () => body };
}

export default function () {
  for (const score of [0, 0.25, 1]) {
    verify(`accept probability ${score}`, validPrediction(response({ probability: score, model_version: '1' })));
  }
  for (const score of [-0.1, 1.1, NaN, Infinity, true, '0.5', null, undefined]) {
    verify(`reject probability ${String(score)}`, !validPrediction(response({ probability: score, model_version: '1' })));
  }
  for (const body of [null, [], {}, { probability: 0.5, model_version: '2' }, { probability: 0.5, model_version: 1 }]) {
    verify(`reject body ${JSON.stringify(body)}`, !validPrediction(response(body)));
  }
  verify('reject HTTP failure', !validPrediction(response({}, 503)));
  verify('reject malformed JSON', !validPrediction({ status: 200, json() { throw new Error('bad JSON'); } }));
  verify('reject wrong version header', !validPrediction(response({ probability: 0.5, model_version: '1' }, 200, '2')));
  verify('fixed target', loadOptions.thresholds.predict_latency_ms[0] === 'p(95)<200');
  verify('zero-error threshold', loadOptions.thresholds.predict_failures[0] === 'rate==0');
  verify('p99 is collected', loadOptions.summaryTrendStats.includes('p(99)'));
  verify('starter payload', JSON.parse(payload).temp_c === 78.4 && Object.keys(JSON.parse(payload)).length === 6);

  const data = { state: { testRunDurationMs: 2000 }, metrics: {
    predict_attempts: { values: { count: 10 } },
    predict_failures: { values: { passes: 2, fails: 7 } },
    predict_latency_ms: { values: { med: 100, 'p(95)': 250, 'p(99)': 300 } },
    predict_timeouts: { values: { count: 1 } },
  } };
  const saved = JSON.parse(handleSummary(data)['/results/summary.json']);
  verify('native metrics preserved', saved.k6.metrics.predict_attempts.values.count === 10);
  verify('errors and unfinished counted', saved.lab3.error_count === 2 && saved.lab3.unfinished_requests === 1);
  verify('completed-request throughput', saved.lab3.requests_per_second === 4.5);
  verify('error denominator and timeout', saved.lab3.error_rate === 2 / 9 && saved.lab3.timeout_count === 1);
  verify('all percentiles kept', saved.lab3.p50_ms === 100 && saved.lab3.p95_ms === 250 && saved.lab3.p99_ms === 300);
  verify('failed round never passes', saved.lab3.meets_target === false);
  const empty = JSON.parse(handleSummary({ state: { testRunDurationMs: 1 }, metrics: {} }).stdout);
  verify('no requests is not success', empty.meets_target === false && empty.error_rate === null && empty.p95_ms === null);
}
