# Ticketing web UI

Use Node 22.12 or newer on the Node 22 line. From the repository root:

```sh
cd frontend
export npm_config_cache="$PWD/.npm-cache"
npm ci
npm run dev
```

Open Vite's local URL with `?mock=1` for a 10,000-seat live demo. Open `?mock=1#/login` to choose a buyer, viewer or admin. Demo changes are held in memory, and Excel exports require the backend.

`npm run build` writes the production site to `frontend/dist`. Serve that directory at the nginx site root alongside the same-origin `/api/v1` and `/pay/` routes. Disable proxy buffering for the seat, metrics and alert event streams and forward `Last-Event-ID` on the seat stream. The seat-import route needs a request body limit of at least 21 MiB to pass a 20 MiB file with multipart headers. For connected Vite development, set `TICKETING_ORIGIN` to the upstream origin (defaults to `http://localhost:8080`) and allow the Vite page's exact origin in the API's Origin configuration.

## Verification

```sh
npm run typecheck
npm run test
npm run build
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.npm-cache/browsers"
npm exec playwright install chromium
npm run test:e2e
```

Set `TICKETING_SMOKE_ORIGIN` to the integrated nginx origin and run `npm run test:e2e` for the real-stack smoke suite. Set `TICKETING_SMOKE_EVENT_ID` to a published event with imported seats and supply `TICKETING_SMOKE_<ROLE>_EMAIL` and `TICKETING_SMOKE_<ROLE>_PASSWORD` for each of `USER`, `VIEWER` and `ADMIN`. The default smoke suite reads app data and creates and revokes its login sessions. On a disposable stack, set `TICKETING_SMOKE_WRITE=1` to also create an event and complete a mock payment. This test leaves the resulting event and paid order in the test database.

## API integration

- Event ownership uses `owner_id`. Sort options send `sales_start_at`, `-created` and `price_min`. Updates and publication use the ETag from the detail request. Imports accept `progress_percent` or `progress`.
- Seat replay uses a numeric sequence in `Last-Event-ID`. The adapter accepts `last_seq` or `last_assigned_seq` on snapshots and `head` or `assigned_head` on progress frames.
- The order client accepts `public_id` or `id` as the public order identifier. The order screen uses `pending_payment` for a hold and `refund_pending` for a refund in progress. `event_title`, `zone_code`, `row_label` and `seat_number` enrich order displays when present.
- The monitoring client reads error rates and completeness as fractions. It accepts both single-alert updates and alert snapshots, and supports grouped metrics and per-source history rows. It combines `access_log` and `service_logs` for the trace table. Field mappings and endpoint paths live in `src/api.ts`.
