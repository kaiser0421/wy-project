// Run with k6 and https://github.com/phymbert/xk6-sse.
// Use sse_client.py when the extension is unavailable or for the final PostgreSQL audit.
import { check, sleep } from 'k6';
import { SharedArray } from 'k6/data';
import exec from 'k6/execution';
import http from 'k6/http';
import { Counter, Trend } from 'k6/metrics';
import sse from 'k6/x/sse';

const url = (__ENV.BASE_URL || 'http://127.0.0.1:8080') + '/api/v1/events/' + __ENV.EVENT_ID;
const vus = Number(__ENV.CONNECTIONS || 2000);
const tokens = new SharedArray('scenario-b-sessions', () =>
  JSON.parse(open(__ENV.SESSIONS_FILE))
);
if (tokens.length < vus || new Set(tokens.slice(0, vus)).size !== vus) {
  throw new Error('Scenario B requires a distinct session per VU');
}

export const options = {
  scenarios: {
    seat_streams: {
      executor: 'constant-vus',
      vus,
      duration: __ENV.DURATION || '75s',
      gracefulStop: '0s',
    },
  },
  summaryTrendStats: ['p(50)', 'p(95)', 'p(99)'],
};
const connected = new Counter('seat_stream_connected');
const latency = new Trend('seat_apply_ms', true);
const snapshots = new Trend('seat_snapshot_ms', true);
const gaps = new Counter('seat_stream_gaps');
const resyncs = new Counter('seat_stream_resyncs');

let applied = 0;
let requiredLayout = 0;
let needsSnapshot = true;
let seats;

export default function () {
  const id = exec.vu.idInTest;
  const cookie = (__ENV.COOKIE_NAME || 'sid') + '=' + tokens[id - 1];
  if (needsSnapshot) {
    const response = http.get(url + '/seats?format=compact&min_layout_ver=' + requiredLayout, {
      headers: { Cookie: cookie },
      responseType: 'text',
    });
    if (!check(response, { 'snapshot 200': (r) => r.status === 200 })) {
      sleep(0.2);
      return;
    }
    snapshots.add(response.timings.duration);
    const body = response.json();
    if (body.layout_ver < requiredLayout) {
      sleep(0.2);
      return;
    }
    seats = new Map(body.seats.map((seat, pos) =>
      [seat.idx, [body.status[pos], body.versions[pos]]]
    ));
    applied = body.last_seq;
    needsSnapshot = false;
  }

  const response = sse.open(
    url + '/seat-stream',
    { method: 'GET', headers: { Cookie: cookie, 'Last-Event-ID': String(applied) } },
    (client) => {
      client.on('open', () => connected.add(1));
      client.on('event', (frame) => {
        if (frame.name === 'resync' || frame.name === 'layout') {
          const next = frame.name === 'layout' ? Number(frame.id) : applied + 1;
          if (next > applied + 1) {
            gaps.add(1);
          } else if (next > applied) {
            resyncs.add(1);
            if (frame.name === 'layout') requiredLayout = JSON.parse(frame.data).layout_ver;
            needsSnapshot = true;
          } else {
            return;
          }
          client.close();
        } else if (frame.name === 'delta') {
          const delta = JSON.parse(frame.data);
          if (delta.from > applied + 1 || Number(frame.id) !== delta.to) {
            gaps.add(1);
            client.close();
            return;
          }
          if (delta.to <= applied) return;
          for (const [idx, status, version] of delta.changes) {
            const previous = seats.get(idx);
            if (!previous || previous[1] < version) seats.set(idx, [status, version]);
          }
          applied = delta.to;
          latency.add(Date.now() - Date.parse(delta.ts));
        }
        if (id % 10 === 0) sleep(Number(__ENV.SLOW_DELAY || 0.25));
      });
    }
  );
  check(response, { 'stream 200': (r) => r && r.status === 200 });
  sleep(Math.random() * 0.2 + 0.05);
}
