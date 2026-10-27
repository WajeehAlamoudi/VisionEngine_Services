# Row-builder / buffer handoff

Measured locally on 27 October 2026. No Jetson service, backend, live database,
repository commit or remote push was changed. These results do not certify
production capacity.

## Environment and scope

- Windows 11 (10.0.26300), Intel64 Family 6 Model 140; Python 3.12.6.
- SQLite 3.45.3, WAL and synchronous=FULL for both producer journal and buffer.
- Isolated temporary files, a 200 MiB main-buffer limit, normal 1000-batch /
  4 MiB journal limits, background maintenance every second.
- Synthetic rows contain event/object IDs, camera, label, confidence, geometry and
  a 100-character context field. Capacity probe uses 2000 context characters.
- Existing ingest claim size is 50 except the explicit outage recovery probe,
  which claims 1000 rows to verify draining. Backend is simulated as immediate
  success. There is no real backend HTTP/database, GPU or camera time.
- Storage benchmarks call journal/buffer APIs directly; the separately labelled
  TCP case uses real loopback ZeroMQ, the actual delivery thread, checkpoint RPC,
  and an independent maintenance connection.
- Times are observations from individual runs, not latency upper bounds.
  SQLite footprint values are sampled, not a filesystem-quota guarantee.

## Correctness checks

The temporary test and benchmark scripts were removed at the user's request
after these checks ran. Results below are historical measurements; this file
does not ship executable benchmark commands.

**17 checks passed**, including:

1. Same UUID/hash retry inserts once; conflicting content is rejected.
2. Invalid hash and producer sequence gaps do not save data.
3. Producer retirement is monotonic, scoped per producer, and cannot acknowledge
   a future unsaved sequence.
4. Retirement can remove source records while undelivered payload stays intact.
5. Source ACK loss and later replay after backend ACK do not insert twice.
6. Producer checkpoint, UUID and pending journal survive restart.
7. Backend grouping/order/UUID survive buffer restart, release and lease expiry.
8. Stale lease ACK cannot delete newly leased rows.
9. Completed ACK replay metadata stays at or below 1024 tokens and ages out.
10. Capacity rejection is atomic; maintenance does not evict undelivered rows;
    draining frees reusable capacity and intake resumes.
11. Background maintenance runs with serving writes without errors.
12. Real TCP handoff retries after an actual response timeout; idle retirement
    removes the final source record.
13. Actual buffer serving/status/shutdown loop works with the maintenance thread.
14. A 99-entry journal drains and retires correctly.
15. Exactly 1000 items or the 4 MiB payload ceiling blocks another append until
    an ACK frees space; already journaled data remains present.
16. Old buffer schemas are refused without modification.
17. Lowering the configured limit below an existing main file fails explicitly
    rather than silently accepting SQLite's larger effective page ceiling.

Some checks combined several assertions.

## Measurements

| Scenario | Observed result |
|---|---|
| 1,000,000 rows, 500/source batch, 50/ingest claim | 219.052 seconds; **4565.1 rows/s** |
| Same million-row run: save + journal deletion, per source batch | p50 17.179 ms; p95 21.471 ms; p99 29.390 ms; max 47.592 ms |
| Same million-row run: sampled main file / total SQLite files | 1,507,328 bytes (1.438 MiB) / 2,504,208 bytes (2.388 MiB) |
| Same million-row run: final state | 0 pending rows; 0 source records; 0 pending/completed deliveries; 1024 bounded ACK tokens; 0 evictions |
| Storage-only 99-batch backlog, 50 rows each | 4950 rows; backlog drain 515.082 ms; total including journal fill 622 ms |
| Storage-only 12,000 rows, 150/source batch | 1.254 seconds; 9569 rows/s; save + journal ACK p99 6.325 ms |
| Real TCP 99-batch handoff, initial 4950 rows | **348 ms**, 284.3 batches/s; 0 retries; 0 evictions |
| TCP case: server put latency | p50 1.773 ms; p95 3.896 ms; p99/max 12.352 ms |
| TCP case: simulated ingest drain after handoff | 3.667 seconds (existing claim/ACK RPC clients); 0 rows left |

The TCP handoff initially took 2.518 seconds when a new row-builder REQ socket was
created per request. Reusing successful connections reduced the measured handoff
to 348 ms. Timeout/error paths discard the socket and reconnect; the real lost-ACK
test passed after this change. Ingest's socket/polling implementation is unchanged.

The one-million-row run is **one million rows total, not one million rows/second**.
It was a sequential stream with a maximum journal occupancy of one batch and an
immediate simulated backend. It demonstrates bounded bookkeeping through repeated
drain cycles, not an outage at one-million-row backlog or a rush-hour capacity
certificate. The 99-batch test explicitly starts with a backlog instead.

## Outage / full-buffer test

With backend delivery withheld, a fresh 200 MiB buffer accepted **41,400 larger
rows**, then rejected the next 200-row batch with capacity. At rejection:

- Main/live allocated bytes: **178,241,536** (169.984 MiB).
- Total database + WAL + SHM: **179,448,536** (171.135 MiB).
- Pending rows evicted: **0**.
- Maintenance preserved all 41,400 rows.
- All 41,400 subsequently drained with simulated success.
- The previously rejected 200-row batch then saved successfully without reset.
- Total probe duration: 5.951 seconds.

Admission deliberately stops around 85% occupied usage to leave transaction and
delivery/retirement headroom, with a post-insert rollback threshold of 90%.
Rows per MiB depend on JSON and SQLite/index overhead; these are not universal
row-capacity figures. The hard SQLite page ceiling bounds the main database.
Transient WAL and SHM are additional disk use. A long reader can delay checkpoint
reclamation; disk headroom still requires monitoring.

## Additional maximum-claim measurement

With an immediate simulated backend, 100,000 rows in actual 1000-row producer
batches and 1000-row claims took 6.703 seconds: **14,917.9 rows/s**. Main file
maximum sampled size was 2,359,296 bytes, total SQLite files 3,440,640 bytes.
Journal occupancy peaked at one batch, with zero pending rows and evictions at
completion. Save + journal ACK latency was p50 21.618 ms, p95 23.454 ms and
p99/max 35.020 ms. This direct-storage run excluded network RPC and real HTTP.

## Limits and rollout

The measured path does not meet a one-million-row/second target. Nor do these
measurements establish the earlier hypothetical 240,000 rows/s / 2400 source
batches/s. Those targets require additional batching/concurrency/architecture
work and measurements on the intended hardware and backend.

No finite buffer can guarantee it never fills during an unlimited backend outage
or sustained intake above delivery rate. The fix removes growth with seven days
of completed history; it does not remove this physical constraint. A full buffer
now rejects atomically rather than deleting undelivered rows. The upstream
PUB/SUB queues can still drop before producer journaling when its own bounded
journal is full. End-to-end losslessness would require a separate upstream
durability/backpressure design.

For the agreed development reset, deploy matching row-builder/buffer code with
fresh **both** buffer and producer-journal storage. Old buffer v1/v2 files and
nonempty legacy journals are refused. No automatic reset/migration is included;
no existing local or Jetson database has been deleted. Preserve producer IDs,
sequences and checkpoints across ordinary restarts, and do not restore only one
side from an older backup.
