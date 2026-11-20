import { check, sleep } from 'k6';
import { SharedArray } from 'k6/data';
import exec from 'k6/execution';
import http from 'k6/http';
import { Counter, Rate } from 'k6/metrics';

const base = (__ENV.BASE_URL || 'http://localhost:8080').replace(/\/$/, '');
const origin = __ENV.ORIGIN || base;
const eventId = Number(__ENV.EVENT_ID);
const runId = __ENV.RUN_ID || 'scenario-a';
const vus = Number(__ENV.VUS || '3000');
const sessions = new SharedArray('buyer sessions', () =>
  JSON.parse(open(__ENV.SESSIONS_FILE || './sessions.json')));
const inventory = new SharedArray('event inventory', () =>
  JSON.parse(open(__ENV.INVENTORY_FILE || './inventory.json')));
const zones = [...new Set(inventory.map((seat) => seat.zone_code))];

if (!Number.isSafeInteger(eventId) || eventId < 1 || sessions.length < vus || zones.length === 0) {
  throw new Error('Set EVENT_ID and provide enough sessions and inventory for the scenario.');
}

export const options = {
  scenarios: {
    rush: { executor: 'constant-vus', vus, duration: __ENV.DURATION || '60s' },
  },
  systemTags: ['status', 'method', 'name', 'scenario', 'expected_response'],
  thresholds: {
    unexpected_errors: ['rate==0'],
    checks: ['rate==1'],
  },
};

const outcomes = new Counter('scenario_a_outcomes');
const modes = new Counter('scenario_a_iterations');
const unexpected = new Rate('unexpected_errors');
const writeStatuses = http.expectedStatuses(200, 201, 409);
const businessConflicts = new Set([
  'seat-contended', 'seat-unavailable', 'allocation-contended', 'insufficient-seats',
  'purchase-limit-exceeded', 'event-not-on-sale', 'idempotency-in-progress',
  'order-not-cancellable', 'order-not-payable', 'intent-closed',
]);
const retryable = new Set(['seat-contended', 'allocation-contended', 'idempotency-in-progress']);
let browse = false;

function pick(items) {
  return items[Math.floor(Math.random() * items.length)];
}

function think() {
  sleep(0.05 + Math.random() * 0.25);
}

function key(operation) {
  return `${runId}-${exec.vu.idInTest}-${exec.vu.iterationInScenario}-${operation}`;
}

function request(method, path, name, session, body, idempotencyKey) {
  const headers = { Origin: origin, 'X-Load-Run': runId };
  if (session) {
    headers.Cookie = session.cookie;
    headers['X-CSRF-Token'] = session.csrf_token;
  }
  if (body) headers['Content-Type'] = 'application/json';
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
  const response = http.request(method, base + path, body || null, {
    headers,
    tags: { name },
    responseCallback: writeStatuses,
    timeout: '5s',
  });
  let code = '';
  if (response.status === 409) {
    try {
      code = response.json('code');
    } catch (_) {
      code = '';
    }
  }
  const business = response.status === 409 && businessConflicts.has(code);
  const success = response.status === 200 || response.status === 201;
  const result = success ? 'ok' : business ? 'business_conflict' : 'unexpected';
  outcomes.add(1, { name, result, code });
  unexpected.add(result === 'unexpected', { name });
  return { response, code };
}

function chooseBody() {
  const quantity = 1 + Math.floor(Math.random() * Math.min(4, inventory.length));
  if (Math.random() >= 0.7) return { zone_code: pick(zones), quantity };
  const ids = new Set();
  while (ids.size < quantity) ids.add(pick(inventory).id);
  return { seat_ids: Array.from(ids) };
}

function buy(session, body, idem) {
  const path = `/api/v1/events/${eventId}/orders`;
  let result = request('POST', path, 'create_order', session, body, idem);
  for (let n = 0; n < 2 && retryable.has(result.code); n++) {
    const retryAfter = Number(result.response.headers['Retry-After']);
    sleep(retryAfter > 0 ? retryAfter : 0.1 + Math.random() * 0.2);
    result = request('POST', path, 'create_order', session, body, idem);
  }
  return result;
}

function pay(session, path) {
  const { response } = request('POST', path + '/payments', 'create_payment', session, null, key('pay'));
  if (response.status !== 201) return;
  const checkout = response.json('checkout_url').replace(/^https?:\/\/[^/]+/, '');
  think();
  const confirmed = request('POST', checkout + '/confirm', 'confirm_payment', null);
  if (confirmed.response.status !== 200) return;
  for (let n = 0; n < 12; n++) {
    sleep(0.25);
    const latest = request('GET', path, 'poll_order', session);
    if (latest.response.status === 200 && latest.response.json('status') === 'paid') return;
  }
  outcomes.add(1, { name: 'poll_order', result: 'callback_pending', code: '' });
}

export default function () {
  const session = sessions[exec.vu.idInTest - 1];
  if (browse) {
    modes.add(1, { mode: 'browse' });
    request('GET', '/api/v1/orders', 'list_orders', session);
    sleep(1 + Math.random());
    return;
  }
  modes.add(1, { mode: 'buy' });
  const idem = key('order');
  const body = JSON.stringify(chooseBody());
  const result = buy(session, body, idem);
  if (result.response.status !== 201) {
    if (result.response.status === 0) {
      console.log(JSON.stringify({ unanswered: true, user_id: session.user_id, key: idem }));
    }
    if (result.code === 'purchase-limit-exceeded') {
      const orders = request('GET', '/api/v1/orders?limit=100', 'list_orders', session);
      if (orders.response.status === 200) {
        const tickets = orders.response.json('items')
          .filter((order) => order.event_id === eventId &&
            ['pending_payment', 'paid', 'refund_pending'].includes(order.status))
          .reduce((total, order) => total + order.items.length, 0);
        browse = tickets >= 4;
      }
    }
    think();
    return;
  }

  const publicId = result.response.json('public_id');
  const path = `/api/v1/orders/${publicId}`;
  const manifest = {
    user_id: session.user_id, key: idem, event_id: eventId, public_id: publicId,
    resend_attempted: Math.random() < 0.02,
  };
  if (manifest.resend_attempted) {
    const resend = request('POST', `/api/v1/events/${eventId}/orders`, 'replay_order', session, body, idem);
    manifest.resend_status = resend.response.status;
    manifest.resend_public_id = resend.response.status === 201 ? resend.response.json('public_id') : null;
    check(resend.response, {
      'booking replay returns the original order': (r) =>
        r.status === 201 && r.headers['Idempotent-Replayed'] === 'true' &&
        manifest.resend_public_id === publicId,
    });
  }
  console.log(JSON.stringify(manifest));
  think();
  const action = Math.random();
  if (action < 0.8) {
    pay(session, path);
  } else if (action < 0.9) {
    request('POST', path + '/cancel', 'cancel_order', session, null, key('cancel'));
  }
  think();
}
