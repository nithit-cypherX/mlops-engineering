// Offline checks in the pinned k6 runtime; HTTP below is stubbed.
import { check } from 'k6';
import checkCases from './payload-cases.test.js';
import { payloadCases } from '../loadtest/payload-cases.js';
import { options as comparisonOptions, parseTiming, inspectResponse, runComparison, handleSummary } from '../loadtest/payload-compare.js';

export const options = { vus: 1, iterations: 1, thresholds: { checks: ['rate==1'] } };
function verify(name, condition) { check(condition, { [name]: value => value === true }); }
const goodHeader = 'json_decode;dur=2.000000, scoring;dur=3.000000, processing;dur=10.000000';
function response(header = goodHeader, probability = 0.68) {
  return { status: 200, headers: { 'Server-Timing': header, 'X-Model-Version': '1', 'X-Request-Id': 'offline' },
    timings: { duration: 40 }, json: () => ({ probability, model_version: '1' }) };
}

export default function () {
  checkCases();
  verify('one user, one iteration, hard 180s limit', comparisonOptions.scenarios.comparison.vus === 1
    && comparisonOptions.scenarios.comparison.iterations === 1
    && comparisonOptions.scenarios.comparison.maxDuration === '180s'
    && comparisonOptions.scenarios.comparison.gracefulStop === '0s');
  verify('redirects disabled', comparisonOptions.maxRedirects === 0);
  verify('84 requests with warm-up separated', comparisonOptions.thresholds.http_reqs[0] === 'count==84'
    && comparisonOptions.thresholds['http_reqs{phase:warmup}'][0] === 'count==4'
    && comparisonOptions.thresholds['http_reqs{phase:measured}'][0] === 'count==80');
  const parsed = parseTiming(goodHeader);
  verify('decode three durations and per-request share', parsed.json_decode_ms === 2
    && parsed.scoring_ms === 3 && parsed.processing_ms === 10 && parsed.json_share === 0.2);
  verify('metric order is irrelevant', parseTiming('processing;dur=10, json_decode;dur=2, scoring;dur=3').json_share === 0.2);
  verify('zero components allowed with positive processing', parseTiming('json_decode;dur=0, scoring;dur=0, processing;dur=1').json_share === 0);
  verify('sum allows only six-decimal rounding tolerance', parseTiming('json_decode;dur=0.333334, scoring;dur=0.666667, processing;dur=1') !== null);
  const invalidHeaders = [
    null, undefined, 123, '', 'json_decode;dur=2',
    goodHeader + ', processing;dur=10',
    'json_decode;dur=2, json_decode;dur=3, processing;dur=10',
    'json_decode;dur=-1, scoring;dur=3, processing;dur=10',
    'json_decode;dur=NaN, scoring;dur=3, processing;dur=10',
    'json_decode;dur=Infinity, scoring;dur=3, processing;dur=10',
    'json_decode;dur=2oops, scoring;dur=3, processing;dur=10',
    'json_decode;dur=, scoring;dur=3, processing;dur=10',
    'json_decode;dur=2, scoring;dur=3, processing;dur=0',
    'json_decode;dur=11, scoring;dur=0, processing;dur=10',
    'json_decode;dur=0, scoring;dur=11, processing;dur=10',
    'json_decode;dur=6, scoring;dur=5, processing;dur=10',
    'json_decode;dur=2, other;dur=3, processing;dur=10',
  ];
  invalidHeaders.forEach((value, i) => verify('reject invalid timing header ' + i, parseTiming(value) === null));
  verify('HTTP time is not server time', inspectResponse(response(), 0.68).http_ms === 40
    && inspectResponse(response(), 0.68).server.processing_ms === 10);
  verify('probability tolerance accepted', inspectResponse(response(goodHeader, 0.6800000005), 0.68).valid);
  verify('different prediction rejected', inspectResponse(response(goodHeader, 0.69), 0.68).reason === 'prediction-mismatch');
  verify('missing header is not zero time', inspectResponse(response(''), 0.68).server === null
    && inspectResponse(response(''), 0.68).reason === 'invalid-server-timing');
  verify('timeout retained', inspectResponse({ status: 0, error_code: 1050, headers: {}, timings: { duration: 10000 } }, 0.68).timeout);
  verify('wrong HTTP status rejected', !inspectResponse({ ...response(), status: 503 }, 0.68).valid);
  verify('wrong body version rejected', !inspectResponse({ ...response(), json: () => ({ probability: 0.68, model_version: '2' }) }).valid);
  verify('wrong header version rejected', !inspectResponse({ ...response(), headers: { 'Server-Timing': goodHeader, 'X-Model-Version': '2' } }).valid);
  verify('invalid JSON rejected', !inspectResponse({ ...response(), json() { throw new Error('bad JSON'); } }).valid);
  for (const value of [undefined, NaN, Infinity, -1, '40']) {
    verify('reject invalid HTTP duration ' + String(value), !inspectResponse({ ...response(), timings: { duration: value } }).valid);
  }

  let calls = [], records = [], fault = null, faultAt = 5;
  const post = (url, body, params) => {
    calls.push({ url, body, params });
    if (calls.length === faultAt) {
      if (fault === 'header') return response('');
      if (fault === 'mismatch') return response(goodHeader, 0.8);
      if (fault === 'timeout') return { status: 0, error_code: 1050, headers: {}, timings: { duration: 10000 } };
      if (fault === 'too-large') return { ...response(), status: 413 };
      if (fault === 'server-error') return { ...response(), status: 503 };
      if (fault === 'body-version') return { ...response(), json: () => ({ probability: 0.68, model_version: '2' }) };
      if (fault === 'header-version') return { ...response(), headers: { 'Server-Timing': goodHeader, 'X-Model-Version': '2' } };
      if (fault === 'http-timing') return { ...response(), timings: { duration: NaN } };
      if (fault === 'invalid-json') return { ...response(), json() { throw new Error('bad JSON'); } };
    }
    return response();
  };
  runComparison(post, item => records.push(item));
  verify('exactly 84 posts and records', calls.length === 84 && records.length === 84);
  verify('same endpoint, no redirects/retries, 10s timeout', calls.every(c => c.url === __ENV.TARGET
    && c.params.timeout === '10s' && c.params.redirects === 0 && c.params.headers['Content-Type'] === 'application/json'));
  verify('four warm-ups first', records.slice(0, 4).every((r, i) => r.phase === 'warmup'
    && r.round === 0 && r.case === payloadCases[i].name));
  verify('20 rotating rounds use prepared bodies', calls.slice(4).every((c, i) => {
    const item = payloadCases[(Math.floor(i / 4) + i % 4) % 4];
    return c.body === item.body && c.params.tags.case === item.name && records[i + 4].bytes === item.bytes
      && records[i + 4].round === Math.floor(i / 4) + 1 && records[i + 4].phase === 'measured';
  }));
  for (const item of payloadCases) {
    verify(item.name + ': 20 measured and five at each position', records.filter(r => r.phase === 'measured' && r.case === item.name).length === 20
      && [0, 1, 2, 3].every(p => calls.slice(4).filter((c, i) => i % 4 === p && c.params.tags.case === item.name).length === 5));
  }
  verify('all valid outputs retained', records.every(r => r.valid && r.probability === 0.68 && r.server.json_share === 0.2));
  for (const mode of ['header', 'mismatch', 'timeout', 'too-large', 'server-error',
    'body-version', 'header-version', 'http-timing', 'invalid-json']) {
    // Last warm-up, first measurement, middle of the run, and final request.
    for (const position of [4, 5, 40, 84]) {
      calls = []; records = []; fault = mode; faultAt = position;
      runComparison(post, r => records.push(r));
      verify(mode + ' at ' + position + ': keep failure then stop without retry', calls.length === position
        && records.length === position && records.filter(r => !r.valid).length === 1
        && !records[position - 1].valid && (mode !== 'timeout' || records[position - 1].timeout));
    }
  }
  calls = []; records = []; fault = 'header'; faultAt = 1;
  runComparison(post, r => records.push(r));
  verify('bad warm-up stops before measurements', calls.length === 1 && records.length === 1 && !records[0].valid);

  const metrics = {
    http_reqs: { values: { count: 84 } }, 'http_reqs{phase:warmup}': { values: { count: 4 } },
    'http_reqs{phase:measured}': { values: { count: 80 } }, iterations: { values: { count: 1 } },
    payload_attempts: { values: { count: 84 } }, payload_completed: { values: { count: 84 } },
    payload_errors: { values: { count: 0 } }, payload_timeouts: { values: { count: 0 } },
    checks: { values: { passes: 84, fails: 0 } },
  };
  for (const [i, item] of payloadCases.entries()) {
    metrics['payload_measured{case:' + item.name + '}'] = { values: { count: 20 } };
    for (const [key, value] of Object.entries({
      http_ms: 40, json_decode_ms: [1, 4, 5, 6][i], scoring_ms: 1, processing_ms: 10, json_share: [0.1, 0.4, 0.5, 0.6][i],
    })) {
      metrics['payload_' + key + '{case:' + item.name + '}'] = { values: { count: 20, min: value, med: value, max: value } };
    }
  }
  const summarize = m => JSON.parse(handleSummary({ metrics: m })['/results/summary.json']);
  const clone = () => JSON.parse(JSON.stringify(metrics));
  const saved = summarize(metrics);
  verify('valid summary preserves native data', saved.lab3.valid_comparison && saved.k6.metrics.http_reqs.values.count === 84);
  verify('50 percent itself is not a majority', !saved.lab3.cases[2].json_majority
    && saved.lab3.cases[3].json_majority && saved.lab3.first_tested_json_majority_bytes === 16777216);
  verify('lowest tested majority, not exact crossover', saved.lab3.conclusion === 'json-majority-observed');
  const ratios = clone();
  ratios['payload_json_share{case:' + payloadCases[2].name + '}'].values = { count: 20, min: 0.2, med: 0.35, max: 0.5 };
  ratios['payload_json_decode_ms{case:' + payloadCases[2].name + '}'].values = { count: 20, min: 1, med: 2.5, max: 4 };
  ratios['payload_processing_ms{case:' + payloadCases[2].name + '}'].values = { count: 20, min: 2, med: 11, max: 20 };
  verify('median of request shares, not ratio of medians', summarize(ratios).lab3.cases[2].median_json_share === 0.35);
  const noMajority = clone();
  noMajority['payload_json_share{case:' + payloadCases[3].name + '}'].values = { count: 20, min: 0.4, med: 0.4, max: 0.4 };
  verify('valid no-majority result is explicit', summarize(noMajority).lab3.conclusion === 'not-observed-in-tested-range'
    && summarize(noMajority).lab3.first_tested_json_majority_bytes === null);
  for (const key of ['payload_errors', 'payload_timeouts']) {
    const broken = clone(); broken[key].values.count = 1;
    verify(key + ': no conclusion or medians', !summarize(broken).lab3.valid_comparison
      && summarize(broken).lab3.conclusion === 'invalid-comparison'
      && summarize(broken).lab3.cases.every(c => c.median_http_ms === null && c.json_majority === null));
  }
  for (const key of ['http_reqs', 'http_reqs{phase:warmup}', 'http_reqs{phase:measured}', 'iterations',
    'payload_attempts', 'payload_completed', 'payload_measured{case:' + payloadCases[0].name + '}']) {
    const broken = clone(); broken[key].values.count -= 1;
    verify(key + ': incomplete cannot pass', !summarize(broken).lab3.valid_comparison);
  }
  const interrupted = clone(); interrupted.payload_completed.values.count = 83;
  verify('unfinished request counted', summarize(interrupted).lab3.unfinished_requests === 1);
  for (const key of ['payload_errors', 'payload_timeouts', 'payload_json_decode_ms{case:' + payloadCases[0].name + '}']) {
    const broken = clone(); delete broken[key];
    verify(key + ': missing evidence cannot pass', !summarize(broken).lab3.valid_comparison);
  }
  for (const change of [{ count: 19 }, { med: NaN }, { min: -1 }, { med: 99 }]) {
    const broken = clone();
    Object.assign(broken['payload_json_share{case:' + payloadCases[0].name + '}'].values, change);
    verify('invalid trend stats ' + JSON.stringify(change), !summarize(broken).lab3.valid_comparison);
  }
  const failedCheck = clone(); failedCheck.checks.values.fails = 1;
  verify('failed native check prevents pass', !summarize(failedCheck).lab3.valid_comparison);
  verify('empty summary is not a no-majority result', summarize({}).lab3.conclusion === 'invalid-comparison');
}
