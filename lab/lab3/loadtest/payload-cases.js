// Synthetic byte-size cases: same one-row input, only JSON whitespace grows.
// This prepares bodies; it does not send requests or measure serialization.
import { payload } from './k6.js';

export const payloadCases = [
  { name: 'original', bytes: payload.length },
  { name: '10KiB', bytes: 10 * 1024 },
  { name: '100KiB', bytes: 100 * 1024 },
  { name: '1MiB', bytes: 1024 * 1024 },
].map(({ name, bytes }) => ({
  name, bytes,
  // The fixed sensor JSON is ASCII, so one character is one UTF-8 byte.
  // Insert before the closing brace so the parser must consume the padding.
  body: payload.slice(0, -1) + ' '.repeat(bytes - payload.length) + '}',
}));
