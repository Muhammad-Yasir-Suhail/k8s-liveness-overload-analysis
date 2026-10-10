// k6 load generator — Group G18
//
// Constant-arrival-rate (open model), NOT constant-VU. This matters:
// a closed model would self-throttle when pods die, masking the cascade.
// An open model keeps offering the same request rate regardless of how
// many replicas survive, which is what a real client population does.
//
// Env:
//   RATE      requests per second to offer
//   DURATION  test duration, e.g. "10m"
//   TARGET    base URL of the service
//   OUT       summary JSON path

import http from 'k6/http';
import { Trend, Rate, Counter } from 'k6/metrics';

const RATE     = parseInt(__ENV.RATE || '100');
const DURATION = __ENV.DURATION || '10m';
const TARGET   = __ENV.TARGET || 'http://localhost:8080';

export const errRate    = new Rate('sut_errors');
export const timeouts   = new Counter('sut_timeouts');
export const serverErrs = new Counter('sut_5xx');
export const latency    = new Trend('sut_latency', true);

export const options = {
  discardResponseBodies: true,
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      // Generous pre-allocation so k6 itself never becomes the bottleneck.
      preAllocatedVUs: Math.max(50, RATE * 2),
      maxVUs: Math.max(200, RATE * 8),
    },
  },
  // No thresholds that abort — we WANT to observe failure, not stop on it.
};

export default function () {
  const res = http.get(`${TARGET}/work`, { timeout: '10s' });

  latency.add(res.timings.duration);

  if (res.status === 0) {
    timeouts.add(1);
    errRate.add(true);
  } else if (res.status >= 500) {
    serverErrs.add(1);
    errRate.add(true);
  } else {
    errRate.add(false);
  }
}

export function handleSummary(data) {
  const out = __ENV.OUT || 'summary.json';
  return { [out]: JSON.stringify(data, null, 2) };
}
