// Offline preparation checks only: no HTTP requests or performance claims.
import { check } from 'k6';
import { payload } from '../loadtest/k6.js';
import { payloadCases } from '../loadtest/payload-cases.js';

export const options = { vus: 1, iterations: 1, thresholds: { checks: ['rate==1'] } };
function verify(name, condition) { check(condition, { [name]: value => value === true }); }

export default function () {
  verify('four fixed sizes from 1 to 8 MiB', JSON.stringify(payloadCases.map(c => c.bytes))
    === JSON.stringify([1048576, 2097152, 4194304, 8388608]));
  verify('four unique labels', new Set(payloadCases.map(c => c.name)).size === 4);
  verify('labels match the approved sizes', JSON.stringify(payloadCases.map(c => c.name))
    === JSON.stringify(['1MiB', '2MiB', '4MiB', '8MiB']));
  verify('1 MiB overlaps the previous experiment with the same body', payloadCases[0].body
    === payload.slice(0, -1) + ' '.repeat(1048576 - payload.length) + '}');
  verify('84 planned requests total 315 MiB of bodies', payloadCases.reduce((n, c) => n + c.bytes, 0) * 21
    === 315 * 1048576);
  const original = JSON.parse(payload);
  for (const item of payloadCases) {
    verify(`${item.name}: ASCII makes character and byte counts equal`, /^[\x00-\x7f]*$/.test(item.body));
    verify(`${item.name}: exact byte size`, item.body.length === item.bytes);
    verify(`${item.name}: only spaces inserted before closing brace`,
      item.body.slice(0, payload.length - 1) === payload.slice(0, -1)
      && item.body.endsWith('}') && /^ *$/.test(item.body.slice(payload.length - 1, -1)));
    const decoded = JSON.parse(item.body);
    verify(`${item.name}: same six keys and values`, Object.keys(decoded).length === 6
      && Object.keys(original).every(key => decoded[key] === original[key]));
    verify(`${item.name}: round trip matches original JSON`, JSON.stringify(decoded) === payload);
  }
}
