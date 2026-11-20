# Payment simulator

The merchant calls `/v1/intents` and `/v1/refunds` over the private network.
Expose only `/pay/` through the public proxy. The checkout page offers Pay
and Decline. Confirm atomically records a single capture while the intent is
open and before its deadline. The same merchant ID or refund ID can be
retried with the original parameters.

A single paymock replica stores intents, captures, refunds, cancellation
markers and the callback queue in SQLite at `/tmp/ticketing-paymock.sqlite3`.
The file survives a process restart on the same filesystem. For container
replacement, set `PAYMOCK_STORE_PATH` to a file on a persistent volume writable
by the paymock user. Start the service with `PAYMENT_WEBHOOK_SECRET` set to the
same value as the backend. Each delivery uses the original event ID and a
new wall-clock timestamp and HMAC. Failed deliveries back off from one second
to fifteen minutes and expire after 24 hours.

| Environment variable | Effect |
| --- | --- |
| `PAYMOCK_DUPLICATE_CALLBACKS=1` | Send a second copy after successful delivery. |
| `PAYMOCK_DELAY_CALLBACK_S` | Delay the first delivery by this many seconds. |
| `PAYMOCK_DROP_CALLBACKS=1` | Pause delivery while retaining the queue and pollable provider state. |
| `PAYMOCK_STORE_PATH` | Override the SQLite file location. |
