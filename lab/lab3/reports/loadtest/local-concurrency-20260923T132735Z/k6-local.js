// Local diagnostic only. Reuse the committed warm workload and timing capture.
import http from 'k6/http';
import { check } from 'k6';
import { Rate } from 'k6/metrics';
import { options as originalOptions, runRequest } from '/work/loadtest/k6.js';
export { setup, handleSummary } from '/work/loadtest/k6.js';

const mismatches = new Rate('prediction_mismatches');
export const options = {
  ...originalOptions,
  thresholds: { ...originalOptions.thresholds, prediction_mismatches: ['rate==0'] },
};

export default function () {
  let probability = null;
  let agrees = false;
  const record = runRequest(
    (...args) => {
      const response = http.post(...args);
      try {
        probability = response.json().probability;
        agrees = typeof probability === 'number' && Number.isFinite(probability)
          && Math.abs(probability - 0.00737357519563027) <= 1e-9;
      } catch (_) { /* Preserve failed response records. */ }
      return response;
    },
    record => console.log(JSON.stringify({
      ...record, probability, matches_reference: agrees,
    })),
  );
  mismatches.add(!agrees);
  check(record, {
    'valid prediction and model version': value => value.prediction_valid,
    'same registered-model prediction': () => agrees,
  });
}
