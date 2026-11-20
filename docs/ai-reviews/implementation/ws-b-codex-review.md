1. **P0 — [compose.yaml:96](/Users/kaiser/workspace/wy_wt/b/compose.yaml:96)** Every XLSX export fails on the default read-only API container. Mount a writable, disk-backed `exports:/data/exports` volume on `api` and declare `exports` under top-level `volumes`.

2. **P1 — [sales.py:301](/Users/kaiser/workspace/wy_wt/b/backend/src/ticketing/reports/sales.py:301)** Viewer draft reports expose zone names, prices, and seat counts. For viewer drafts, retain permitted event metadata and render empty Zones and Orders sheets without querying inventory or sales.

3. **P1 — [pagination.py:93](/Users/kaiser/workspace/wy_wt/b/backend/src/ticketing/events/pagination.py:93)** A demoted admin can reuse a cursor to learn a deleted draft’s ID. The authorization join skips deleted events, but the page emits their IDs. Bind the actor’s role into the cursor HMAC and return `409 cursor-invalidated` on mismatch.

4. **P1 — [imports/runner.py:381](/Users/kaiser/workspace/wy_wt/b/backend/src/ticketing/imports/runner.py:381)** Brief event-lock contention can consume all three attempts and permanently fail a valid upload. Retry finalize on `LockNotAvailableError` under the same fence with bounded backoff, renewing the lease in a separate job-only transaction between retries.

5. **P1 — [parser.py:127](/Users/kaiser/workspace/wy_wt/b/backend/src/ticketing/imports/parser.py:127)** Malformed JSON beyond the decoder’s recursion limit becomes a row error, letting `skip_invalid` commit other rows. Validate deep items with an iterative JSON parser and emit `nesting_too_deep` only after verifying syntax.

6. **P1 — [api/routes/reports.py:67](/Users/kaiser/workspace/wy_wt/b/backend/src/ticketing/api/routes/reports.py:67)** Requests for forbidden events can occupy the local and global export slots before ownership is checked. Authorize the event before acquiring either slot and repeat the check in the workbook snapshot.

7. **P1 — [nginx.conf:96](/Users/kaiser/workspace/wy_wt/b/infra/nginx/nginx.conf:96)** Nginx times out after 60 seconds without headers, while export generation can spend 120 seconds on one DB statement. Give XLSX exports a dedicated location with `proxy_buffering off` and a read timeout above an enforced total export deadline.

8. **P2 — [validation.py:18](/Users/kaiser/workspace/wy_wt/b/backend/src/ticketing/imports/validation.py:18)** The importer rejects all-zero seat numbers despite DESIGN A4. Use `coalesce(nullif(ltrim(seat_number, '0'), ''), '0')` for both normalized keys and inserted seat numbers.

9. **P2 — [parser.py:63](/Users/kaiser/workspace/wy_wt/b/backend/src/ticketing/imports/parser.py:63)** UTF-8 read-ahead can report an invalid byte on line 3 as a line 1 error. Feed `csv.reader` physical lines decoded individually, preserving CR, LF, CRLF, and quoted multiline records.

10. **P2 — [parser.py:41](/Users/kaiser/workspace/wy_wt/b/backend/src/ticketing/imports/parser.py:41)** A valid 5,000-digit JSON integer triggers Python’s conversion limit and fails the whole upload. Use a bounded `parse_int` hook that returns an invalid-field sentinel for oversized integers, so `skip_invalid` can keep processing.