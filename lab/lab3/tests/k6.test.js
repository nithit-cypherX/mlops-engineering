// Offline checks in the real k6 runtime. No HTTP calls, model, or credentials.
import { check } from 'k6';
import { validPrediction, handleSummary, options as loadOptions, payload,
  parseTiming, timingRecord, runRequest } from '../loadtest/k6.js';

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
  verify('no timing is not complete evidence', !empty.timing_evidence.complete
    && empty.timing_evidence.valid_samples === 0 && empty.timing_evidence.invalid_samples === null);

  const header = 'json_decode;dur=2, scoring;dur=3, processing;dur=10';
  const good = { ...response({ probability: 0.68, model_version: '1' }),
    headers: { 'X-Model-Version': '1', 'X-Request-Id': 'request-a', 'Server-Timing': header },
    timings: { duration: 40, sending: 1, waiting: 37, receiving: 2 } };
  const record = timingRecord(good);
  verify('same-request HTTP and server values retained', record.timing_valid
    && record.http.duration_ms === 40 && record.http.waiting_ms === 37
    && record.server.processing_ms === 10 && record.server.scoring_ms === 3
    && record.server.json_decode_ms === 2);
  verify('request identifier and raw header retained', record.request_id === 'request-a'
    && record.server_timing_header === header && record.kind === 'warm-request-timing'
    && record.vu === 1 && record.iteration === 0 && Number.isFinite(Date.parse(record.completed_utc)));
  verify('timing completeness has a separate threshold', loadOptions.thresholds.predict_timing_failures[0] === 'rate==0'
    && loadOptions.thresholds.predict_timing_samples[0] === 'count>0');
  verify('duration and load shape unchanged', loadOptions.vus === 10 && loadOptions.duration === '60s'
    && loadOptions.maxRedirects === 0);
  verify('no unsupported top-level gracefulStop', !Object.hasOwn(loadOptions, 'gracefulStop'));
  const missing = { ...good, headers: { ...good.headers, 'Server-Timing': undefined } };
  const badHeader = { ...good, headers: { ...good.headers, 'Server-Timing': 'processing;dur=10' } };
  const timeout = { status: 0, error_code: 1050, headers: {},
    timings: { duration: 10000, sending: 1, waiting: 9999, receiving: 0 } };
  for (const res of [missing, badHeader]) {
    const incomplete = timingRecord(res);
    verify('missing or malformed server time is not zero', incomplete.prediction_valid
      && !incomplete.timing_valid && incomplete.server === null && incomplete.http.duration_ms === 40);
  }
  for (const requestId of [undefined, '', 123]) {
    verify('missing correlation ID is incomplete: ' + String(requestId),
      !timingRecord({ ...good, headers: { ...good.headers, 'X-Request-Id': requestId } }).timing_valid);
  }
  for (const value of [undefined, -1, NaN, Infinity, '40']) {
    const invalid = timingRecord({ ...good, timings: { ...good.timings, duration: value } });
    verify('invalid HTTP time is null: ' + String(value), !invalid.timing_valid && invalid.http.duration_ms === null);
  }
  verify('partial HTTP timing remains incomplete', !timingRecord({ ...good, timings: { duration: 40 } }).timing_valid);
  verify('timeout keeps duration and error code', !timingRecord(timeout).prediction_valid
    && !timingRecord(timeout).timing_valid && timingRecord(timeout).error_code === 1050
    && timingRecord(timeout).http.duration_ms === 10000 && timingRecord(timeout).server === null);
  verify('HTTP error cannot supply successful timing evidence', !timingRecord({ ...good, status: 503 }).timing_valid);
  verify('bad prediction cannot supply successful timing evidence', !timingRecord({ ...good,
    json: () => ({ probability: 2, model_version: '1' }) }).timing_valid);
  verify('shared parser preserves rounding tolerance', parseTiming('json_decode;dur=0.333334, scoring;dur=0.666667, processing;dur=1') !== null);

  const responses = [good, missing, badHeader, { ...good, status: 503 }, timeout];
  const calls = [], records = [];
  for (const res of responses) {
    runRequest((url, body, params) => { calls.push({ url, body, params }); return res; }, item => records.push(item));
  }
  verify('every completed response has one record; no retries', calls.length === 5 && records.length === 5);
  verify('same target, payload, and timeout', calls.every(call => call.url === __ENV.TARGET
    && call.body === payload && call.params.timeout === '10s' && call.params.headers['Content-Type'] === 'application/json'));
  verify('errors and missing timing records are retained', records[0].timing_valid
    && records.slice(1).every(item => !item.timing_valid) && records[4].error_code === 1050);

  const complete = { state: { testRunDurationMs: 2000 }, metrics: {
    predict_attempts: { values: { count: 2 } },
    predict_failures: { values: { passes: 0, fails: 2 } },
    predict_latency_ms: { values: { med: 40, 'p(95)': 50, 'p(99)': 55 } },
    predict_timing_failures: { values: { passes: 0, fails: 2 } },
    predict_timing_samples: { values: { count: 2 } },
  } };
  for (const [name, value] of Object.entries({ json_decode: 2, scoring: 3, processing: 10 })) {
    complete.metrics['predict_' + name + '_ms'] = { values: {
      count: 2, min: value, med: value, 'p(95)': value, 'p(99)': value, max: value,
    } };
  }
  const summarize = input => JSON.parse(handleSummary(input).stdout);
  const paired = summarize(complete);
  verify('complete timing evidence separate from latency target', paired.meets_target && paired.timing_evidence.complete
    && paired.timing_evidence.valid_samples === 2 && paired.timing_evidence.invalid_samples === 0);
  verify('component distributions retained without subtraction', paired.timing_evidence.server_ms.scoring_ms['p(95)'] === 3
    && paired.timing_evidence.paired_records.includes('console.log'));
  for (const metric of ['predict_timing_failures', 'predict_timing_samples', 'predict_processing_ms']) {
    const incomplete = JSON.parse(JSON.stringify(complete));
    delete incomplete.metrics[metric];
    verify('missing evidence cannot be complete: ' + metric, !summarize(incomplete).timing_evidence.complete);
  }
  const lostSample = JSON.parse(JSON.stringify(complete));
  lostSample.metrics.predict_scoring_ms.values.count = 1;
  verify('incomplete component count cannot pass', !summarize(lostSample).timing_evidence.complete);
  complete.metrics.predict_timing_failures.values = { passes: 1, fails: 1 };
  verify('timing failure is explicit without changing the latency claim', !summarize(complete).timing_evidence.complete
    && summarize(complete).timing_evidence.invalid_samples === 1 && summarize(complete).meets_target);
}
