// Synthetic byte-size cases: same one-row input, only JSON whitespace grows.
// This prepares bodies; it does not send requests or measure serialization.
import { payload } from './k6.js';

export const payloadCases = [
  // Keep the previous run's 1 MiB body as the overlap; cap this run at 16 MiB.
  { name: '1MiB', bytes: 1024 * 1024 },
  { name: '4MiB', bytes: 4 * 1024 * 1024 },
  { name: '8MiB', bytes: 8 * 1024 * 1024 },
  { name: '16MiB', bytes: 16 * 1024 * 1024 },
].map(({ name, bytes }) => ({
  name, bytes,
  // The fixed sensor JSON is ASCII, so one character is one UTF-8 byte.
  // Insert before the closing brace so the parser must consume the padding.
  body: payload.slice(0, -1) + ' '.repeat(bytes - payload.length) + '}',
}));
