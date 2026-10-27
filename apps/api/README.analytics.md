# Analytics component — WP16 / BE-A15

The composition entry point is `app.application:create_app`. It requires the core's
`app.state.auth` and a dedicated `settings.analytics_cursor_key` (bytes or hex,
at least 32 bytes). Missing configuration fails explicitly. The cursor key must
be stable across process restarts and separate from boot/IPC secrets.

`GET /api/v1/live-sessions/{id}/analytics` creates or reuses an immutable PG
snapshot. Snapshot/section/round/CSV requests negotiate
`X-BE-Extension-Version: LW-BE/1.1.0`. Every request checks current
`diagnostics.read` and session ownership. Signed keyset cursors bind all query
context; CSV uses the same snapshot and fixed columns with formula escaping.

Raw guesses, rejected attempts, source messages, ledger entries and latency
observations are aggregated by PostgreSQL (`COUNT DISTINCT`, grouped buckets,
`percentile_disc`). They are not loaded into Python participant sets. Only bounded
round/minute/source results enter the materialized JSON. Quota creation, LRU
reservation and pin changes serialize under an installation-wide PG advisory
lock acquired before the repeatable-read snapshot starts. Limits are 16 snapshots
per actor/session, 256 installation-wide, 1 GiB total, 20,000 detail rows and 4 MiB
per snapshot. Query copies expire within 24 hours. SQL NULL tombstones release
quota. Private 0600 CSV files are fully generated and checked before headers;
32 MiB / 30-second limits and PG-backed actor/global export slots apply, with
completion/disconnect cleanup.

## Domain integration

The factory sets `service.analytics` and `app.state.analytics`.
`analytics.observe(connection, kind, session_id, facts)` runs in the existing
domain transaction before its final `touch()`. Supported transition observations:
`session_started`, `session_ended`, `session_sealed`, `round_started`, `round_paused`,
`round_resumed`, `round_settled`, `hint_revealed`, `guess_terminal`, `guess_rejected`,
`source_message`. The writer must supply actual UTC timestamps and boot/monotonic
facts; row creation timestamps do not stand in for session/round start times.
Anonymous observations contain no guess text, answers or raw platform UID.

`session_ended` invokes `try_seal_session(connection, session_id)` in that domain
transaction. The hook requires an ENDED session, an end timestamp, all terminal
rounds/guesses/jobs and no unresolved recovery gap. Official sessions additionally
need an explicit persisted gap status. Recovery and commit-observation callers
may invoke the same hook while holding the session business lock, then touch the
counter. GET never seals a session. Explicit final aggregates have a separate
90-day lifetime; rebuilt query copies retain the sealed metric version, business indicators,
minute/round aggregates, coverage and sealed_at. A new snapshot captures the current
MVCC timestamp, session commit counter, Studio sequence and delivery-pending
diagnostics. Existing snapshot IDs remain immutable, including their old pending
counts. Reuse and five-second creation limits still apply.

`after_commit(session_id, guess_id, phase, boot_id, mono_ns)` is called only after
the corresponding successful admission/feedback commit returns. It stores an
independent observation and advances the session transaction counter. Missing or
cross-boot samples remain missing. Sealed aggregates reject late samples.

## Evidence and remaining integration limits

Writer evidence: 8 unit tests; 12 real-PG integration tests, including 25,002 raw
guesses aggregated into one round, pending completion into the original minute,
VOID exclusion, immutable old CSV, cursor tampering, expiry/revocation, rate
limits, pinned capacity, SQL NULL quota release and final aggregate rebuilding.
Run with the assembled API on PYTHONPATH and the dedicated private testing env.
The integration fixture validates database name, database COMMENT marker and
application instance identity before DDL or fixture writes.

Unobserved historical session/round times, hints and latency remain null/unknown.
Core3 reads `outbox_events` directly as an immutable replay log; there is no
independent dispatcher-job queue. Therefore `local_delivery_pending.outbox=0`
describes pending dispatcher jobs, not undelivered replay-log rows. The
`display_effects` field counts only persisted `processed=false` effects. Missing
realtime metadata/table fails with 503 `DEPENDENCY_UNAVAILABLE`, preserving the
integer shape rather than returning null. Nonofficial local/rehearsal/replay
modes create no official ACK tasks, so `ack_outbox=0`. Official snapshots count
actual local `connector_ack_outbox` rows in pending/running/retry. A trusted
`observe_delivery` transaction can supply a persisted local projection; missing
both queue schema and observation is an explicit dependency failure. Remote
fulfillment is null until trusted remote evidence has been persisted locally.
The connector holds the session lock, writes its observations, and calls core
`touch` last, so the query watermark includes the observation commit.

The real composed HTTP tests use the automatic first round created by start,
then exact hit, end and actual received/processed receipts. They verify real
observer timestamps, same-boot commit latency, point totals, pending effects and
immutable older snapshots. No observation rows are seeded for those transitions.

`AnalyticsMaintenanceWorker(analytics, interval_seconds=60)` is an awaitable
startup/shutdown port: `await start()` performs the initial tick; periodic ticks
are serialized; `await stop()` waits for any running use case. Composition binds
`analytics.connector_retention = ConnectorStore.prune_local_payloads` before
starting the worker. This hook runs under the existing session transaction only
with a sealed, persisted anonymous final basis. It preserves connector source,
stream, hash, terminal and ACK references while removing expired raw payloads.

`maintenance_tick` processes at most 100 due sessions with independent
transactions. Query-limit failures have an executable recovery path: anonymous
minute/round rows are materialized through 128-row DB fetch pages, without the
query copy's 20,000-row/4-MiB ceiling. Complete oversized finals remain durable;
subsequent query snapshots still return 413 without truncation. Pending recovery
freezes the original basis timestamp and watermark; no GET performs sealing or
recovery. The real tests materialize 20,001 rounds and a final larger than 4 MiB.

Seven-day cleanup requires that anonymous final basis and protects pending
effects, replay work and ACK jobs. It retains guesses, source deduplication,
ledger, terminal and receipt identities, replacing expired words/identity keys
with domain-separated keyed digests. Expired immutable outbox prefixes are
removed with sequence/hash tombstones, so replay returns the existing gap
response. Processed effect metadata remains while expired payload bodies become
hash tombstones; terminal replay input keeps count/hash/checkpoint but removes
comment text. Thirty-day trace deletion retains expiry tombstones, so it cannot
reopen an expired trace. Ninety-day aggregate deletion retains only terminal and
opaque-reference metadata. Query copies never prolong these policies.

CSV preflight uses a private 0700 product scratch directory and 0600 files.
Disk/quota failures are controlled 503 errors; byte/time failures occur before
attachment headers. Completion and disconnect release the file, pin and export
slot. Startup/periodic maintenance removes only expired owned scratch files.

Query reuse also binds the effective metric definition version; retained final
bases keep their original version. A query copy of a retained final expires at
the earlier of 24 hours and that final's 90-day deadline. Connector stream trace
metadata is injected through `core.connector_trace_metadata`, after authorization.
Quota reservation selects metadata only; round fact preflight counts uncompressed
JSON bytes, so TOAST compression cannot bypass the memory/serialization bound.
