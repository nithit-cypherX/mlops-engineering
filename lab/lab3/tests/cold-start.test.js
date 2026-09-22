// Offline checks in k6. No endpoint requests or Azure access.
import { check } from 'k6';
import { options as coldOptions, requestTimeout, setup, handleSummary } from '../loadtest/cold-start.js';

export const options = { vus: 1, iterations: 1, thresholds: { checks: ['rate==1'] } };
function verify(name, condition) { check(condition, { [name]: value => value === true }); }
function rejected(target, confirmedAt) {
  __ENV.TARGET = target;
  __ENV.ZERO_REPLICAS_CONFIRMED_AT = confirmedAt;
  try { setup(); return false; } catch (_) { return true; }
}

export default function () {
  const scenario = coldOptions.scenarios.first_prediction;
  verify('one request iteration', scenario.executor === 'shared-iterations' && scenario.vus === 1 && scenario.iterations === 1);
  verify('bounded execution', scenario.maxDuration === '130s' && scenario.gracefulStop === '5s');
  verify('120-second timeout', requestTimeout === '120s');
  verify('no redirects', coldOptions.maxRedirects === 0);
  verify('exactly one completed request required', coldOptions.thresholds.http_reqs[0] === 'count==1' && coldOptions.thresholds.cold_completed[0] === 'count==1');
  verify('valid response required', coldOptions.thresholds.checks[0] === 'rate==1');
  verify('no warm latency target', !coldOptions.thresholds.predict_latency_ms);
  verify('no single-sample percentiles', !coldOptions.summaryTrendStats.some(x => x.startsWith('p(')));
  for (const target of ['', 'http://example.com/predict', 'https://user:pass@example.com/predict', 'https://example.com/ready', 'https://example.com/predict?token=x']) {
    verify(`reject unsafe target ${target}`, rejected(target, '2026-09-21T00:00:00Z'));
  }
  verify('require zero-replica metadata', rejected('https://example.com/predict', ''));
  verify('reject invalid timestamp', rejected('https://example.com/predict', 'not-a-date'));
  verify('accept confirmed target without calling it', !rejected('https://example.com/predict', '2026-09-21T00:00:00Z'));
  verify('accept isolated fixture', !rejected('http://127.0.0.1:8080/predict', ''));

  const data = { metrics: {
    http_reqs: { values: { count: 1 } }, cold_completed: { values: { count: 1 } },
    checks: { values: { passes: 1, fails: 0 } },
    cold_http_status: { values: { value: 200 } }, cold_error_code: { values: { value: 0 } },
    cold_client_elapsed_ms: { values: { min: 1500 } }, http_req_duration: { values: { min: 1400 } },
  } };
  let saved = JSON.parse(handleSummary(data)['/results/summary.json']);
  verify('successful observation', saved.lab3.valid_prediction && saved.lab3.outcome === 'valid_prediction');
  verify('full and HTTP timing kept separately', saved.lab3.client_elapsed_ms === 1500 && saved.lab3.http_duration_ms === 1400);
  verify('native evidence retained', saved.k6.metrics.http_reqs.values.count === 1);
  verify('no p95/p99 claim', !('p95_ms' in saved.lab3) && !('p99_ms' in saved.lab3));
  data.metrics.checks.values = { passes: 0, fails: 1 };
  saved = JSON.parse(handleSummary(data).stdout);
  verify('invalid response is not success', !saved.valid_prediction && saved.outcome === 'invalid_response');
  data.metrics.cold_http_status.values.value = 0;
  data.metrics.cold_error_code.values.value = 1050;
  saved = JSON.parse(handleSummary(data).stdout);
  verify('timeout remains a failure', saved.timeout && !saved.valid_prediction && saved.outcome === 'timeout');
  data.metrics.cold_error_code.values.value = 1212;
  saved = JSON.parse(handleSummary(data).stdout);
  verify('transport error is separate', !saved.timeout && saved.outcome === 'transport_error');
  saved = JSON.parse(handleSummary({ metrics: {} }).stdout);
  verify('empty results cannot pass', !saved.valid_prediction && saved.outcome === 'no_result' && saved.client_elapsed_ms === null);
}
