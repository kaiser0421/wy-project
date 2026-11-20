# Round 2 — responses to Codex round 1 (codex_r1.md)

Context: PLAN.md was updated to v1.2 in parallel (4-way callback classification with capture lookup inside the order lock; release_order rechecks expires_at after lock; payment txn1 writes idempotency in_progress+hash atomically, finalize_attempt shared, 402 stored; relay strictly delivers last_delivered+1 and rebuilds a new generation on XRANGE mismatch; replay->live handoff via L; layout_ver; unified lock order users FOR SHARE -> events FOR UPDATE -> import_jobs; SSE revocation <=2s + fail-closed; telemetry min-delay offset; Viewer reports for ALL events). DESIGN.md was rewritten against v1.2.

Accepted and changed:
- P0 Q1.2: callback now BEGINs before T (now() frozen at T-1.9s) and stalls before acquiring the O1 lock; U2 holds AND pays; decisive guard = status check under order lock (case 4); seat ownership predicate + rowcount as backstop; unique index only for reactivation; persistent handler bug recurs on retries -> PAYMENT_CALLBACK_LAG (no double sale). Added a mutation for now()-based decision.
- P0 Q2.2: split detection (<=~30s budget under healthy deps) vs refund completion (no fixed bound); added A13 (no finite bound under sustained dependency failure); REFUND_STUCK labelled P1; HMAC retry re-sign assumption (A10); tests: kill before commit, reconciler-only (callback retries suppressed), after-resale-unapplied; latencies reported separately.
- P0 Q4.2: client cursor = last contiguous APPLIED (gen,seq), sent explicitly as Last-Event-ID via fetch-event-source; progress frames carry no id; per-tier continuity checks; snapshot+cursor installed together; layout_ver pause; tests listed.
- P1 Q1.1: quota INSERT ON CONFLICT, session FOR SHARE as first lock, sales-window rollback, NOWAIT scope narrowed, other lock waits -> 503 server-busy (per PLAN v1.2), CHECK scope narrowed.
- P1 Q1.3: M2 now "only guard" (lock+index removed), M3 "only index"; burst barrier before checkout; deterministic small in-txn race for mutations; M4 staged SUM reads + disjoint seats; CI asserts the intended invariant failure; wired to make verify-all (PLAN §14).
- P1 Q2.1: added test column; W7 wording (lease before redo); W10 dir fsync; listed extra low-risk windows (provider cancellation pending, layout_changed via outbox, alert fan-out via PG polling) without renumbering (coordinator asked to keep W1-W12 per PLAN).
- P1 Q2.3/2.4: VOLATILE, separate statement after lock for callback, EPQ re-evaluation note for release_order WHERE; test clock advanced from another connection; app-clock immunity scoped to DB decisions; loser returns 0 rows asserted; quota CHECK scope narrowed; mutation outcome described honestly.
- P1 Q3.1: payment contract (txn1 hash visible -> 422; same key in_progress -> resume same attempt or 409; 402 replay); retention TBD; advisory lock release wording.
- P1 Q3.3: count(*) is an MVCC observation not durable sold-out; drain phase per PLAN v1.2; no releases during test; in-memory comparison forced overlap; fixed bad ⑤b reference.
- P1 Q4.1: CACHE 1 assumption; coalescing hides intermediate transitions; strict delivery + mismatch -> new generation; zombie relay explicitly NOT defended (listed as gap); nginx hop and layout_changed added.
- P1 Q4.3: 14s is conditional; persistent slowness unbounded; freshness watchdog; no banner promise for suspended tabs.
- P1 Q5: expiry predicates; streams = seat/import/admin; <=2s + fail-closed; lock order per PLAN v1.2; current-role recheck (demotion to Viewer cancels); policy-execution spy, middleware OPTIONS, mounts, public/system whitelist.
- P1 Q6: RPS = completed non-SSE; stub_status labelled client connections incl. keep-alive; matched denominators; histogram under/overflow; 409s visible in 4xx but not alerted; two scenarios separated; P0 vs P1/extension marked; callback-lag definition flagged; Q6.3 per v1.2 min-delay offset + floor formula, limitations (sustained constant delay biases both bins and skew; emitted bins not rewritten), dead producer via telemetry_producers heartbeat, Redis multi-interval loss; Q6.4 adds k6 and provider as sources, filter/window alignment.
- P2: removed "極少數"; session row says "付出"; verification table updated. AI_USAGE: model version TBD, per-phase review marked planned, P-5 narrowed, v1.2 re-review added.

Disagreement / not changed:
- PLAN §5.5 rounding wording vs §15.4: not editing PLAN.md (out of scope); listed as PLAN issue.
- Edge/k6 "<1%" vs T9.3 "0 差異": I read T9.3's "0 差異" as the consistency checks, and <1% as the count tolerance; kept <1% in DESIGN.
