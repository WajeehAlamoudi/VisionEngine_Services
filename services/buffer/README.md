<div align="center">

![Vision Engine Buffer](../../docs/assets/buffer.svg)

**Commit rows locally. Lease pending work. Recover across restarts.**

[![Python](https://img.shields.io/badge/Python-3.12-142A42?style=flat-square&logo=python)](requirements.txt)
[![Docker](https://img.shields.io/badge/Docker-buffer-142A42?style=flat-square&logo=docker)](Dockerfile)
[![Stack](https://img.shields.io/badge/Stack-SQLite_WAL_+_ZeroMQ-142A42?style=flat-square)](#responsibility)

[Project](../../README.md) · [Services](../README.md) · [Configuration](../../config/README.md) · [Handbook](../../docs/README.md)

</div>

## Responsibility

| Property | Value |
|---|---|
| Instance scope | One shared database owner |
| Input | Acknowledged rows.v1 puts |
| Output | Leased batches + status |
| Image | `visionengine-buffer:local` |
| Generated file | `config/.generated/buffer.compose.yaml` |
| Entry point | `python -m services.buffer` |

## Storage owner

Only this process opens buffer SQLite. It accepts detection, notification and
node rows, commits batches with receipts, and serves leased rows to ingest.
No separate SQL-server image is needed. No backend table creation occurs here.

`api.yaml` owns `buffer.path`, `max_size_mb`, `retry_interval_seconds` and
`delete_after_hours`; `device.yaml` supplies identity. `api.buffer.path` resolves
from the runtime working directory. Compose mounts its parent directory on
`buffer_data`, commonly `/app/data/buffer.db`. The database is created at runtime,
not by generation or inside an image. SQLite uses WAL, `synchronous=FULL`, a busy
timeout and transactions. Storage must honor flushes. Legacy monolithic databases
need an explicit schema migration rather than silent reuse.

New puts acknowledge immediately after the receipt and rows durably commit;
row-delivery ACKs likewise return after their transaction. Neither path runs
cleanup before replying. The service schedules cleanup separately every five
seconds, after sending any current RPC reply. SQLite remains owned by one serving
thread; this is separate scheduled work, not concurrent writers. Retention and
lease-expiry indexes keep that work from scanning all historical metadata.
Compaction reclaims up to 1024 pages in one completed SQLite operation rather
than issuing a separate FULL-sync operation for every page.

## Request/reply operations

| Operation | Result |
|---|---|
| `put(batch_id, batch)` | Transactional rows + receipt; repeated identical ID returns receipt |
| `peek(limit, max_bytes)` | Read pending rows without reserving them |
| `claim(limit, lease_seconds, max_bytes)` | Reserve oldest unassigned rows for an existing ingest consumer |
| `claim_delivery(limit, lease_seconds, max_bytes)` | Persist/lease one single-table delivery with a stable UUIDv4 |
| `renew(lease_id, lease_seconds)` | Extend a live lease |
| `ack(lease_id, ids?)` | Mark all/specified members sent after backend confirmation |
| `release(lease_id)` | Return unconfirmed members to pending |
| `stats()` | Pending/inflight/sent, bytes, receipts, losses and counters |

Correlated version-1 JSON requests use ZeroMQ REQ/REP. A timed-out request needs
a fresh request socket; the local Client handles that. Claims return
`buffer.batch.v1` with lease/token/expiry and rows containing ID, table, payload
and creation time. Defaults are 50 rows, 60-second leases and 8 MiB; limits are
1000 rows, 16 MiB and one-hour leases. Expired leases become pending, including
after restart. Late ACKs cannot complete newly claimed rows. Partial and repeated
ACKs are supported within retained metadata.

Rows and stable batch receipts commit together; same ID/different content is
rejected. Receipts remain at least seven days by default and while rows remain.
Deduplication after receipt/row retention expires is not guaranteed. This local
protocol does not provide backend exactly-once delivery.

## Persistent HTTP delivery identity

`claim_delivery` is an opt-in RPC operation for future ingest/backend integration.
It returns `buffer.delivery.v1` with `delivery_id` (UUIDv4), `table`, `lease_id`,
`expires_at` and the existing row envelopes. Empty replies have null IDs/table
and an empty row list. UUID and row membership commit in SQLite before reply.

An additive schema-v2 migration creates `deliveries` with `delivery_id` (primary
key), `table_name`, `row_ids` (ordered JSON IDs), `state`, `created_at` and
`completed_at`. Existing rows, producer receipts and leases are preserved.
Version-1 buffer code cannot open a version-2 database; retain a pre-upgrade
backup if a code rollback is needed.

Released/expired deliveries retain their UUID, order, table and exact payloads
across restarts. A retry uses a new temporary lease but the same delivery UUID.
Existing pending deliveries are retried before preparing new unassigned work;
live leases are skipped. Lower retry limits return `too_large` rather than split
an existing delivery. Retrying consumers must retain compatible row/byte limits.

Use `renew`/`release` with the lease token and `ack` only after backend success.
Saved deliveries require a full ACK; it marks their rows sent and the delivery
completed in one transaction. Repeated ACKs are safe within retained lease
metadata. Pending delivery members cannot be taken by legacy `claim` or evicted
under capacity pressure. `peek` still shows released pending rows. Completed
delivery metadata is retained for `receipt_days`; pending metadata never expires.
`pending_deliveries` is included in stats, health snapshots and published status.

Updated ingest uses `claim_delivery` and sends `delivery_id` as the backend
`batch_id`. Deploy the receipt-aware backend first, then this buffer, then ingest.
The backend must atomically store its receipt and destination rows and echo the
UUID with the confirmed row count. HTTP retries reuse the saved delivery, never
repackage uncertain rows under a new UUID. Legacy `claim` remains available for
older clients but provides no backend delivery identity.

## Capacity and pending meaning

`pending` is all unsent work, including leased rows; `inflight` is the leased
subset. Sent retention uses `delete_after_hours`; zero removes sent rows promptly.
Cleanup accounts for DB/WAL/shared-memory files, checkpoints and reclaims pages.
Under pressure it removes sent rows before oldest pending rows, exposing
`dropped_pending_total`. Active leases, pending deliveries and unexpired receipts are protected; new
writes can be rejected for retry if protected data fills capacity.

`max_size_mb` is an operational target, not a hard filesystem quota. A single batch
must fit within half the configured capacity. Earlier pipeline losses before
producer journaling remain outside this durable boundary.

## Endpoints and inspection

| Endpoint | Container | Host |
|---|---|---|
| RPC puts/claims/ACK/stats | 5564 | 14000 |
| `buffer.status.v1/<device_id>` | 5565 | 15000 |

`buffer.pending` in generated metadata identifies the pull/RPC interface;
`buffer.status` is an observational PUB feed. Buffer does not subscribe to row
builder's diagnostic PUB. It has no upstream startup dependencies.

```console
python -m config.controller --generate
docker compose -f config/.generated/pipeline.compose.yaml up --build -d visionengine-buffer-service
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-buffer-service python -m services.buffer.tools.client --connect tcp://127.0.0.1:5564 --operation stats --format json
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-buffer-service python -m services.buffer.tools.client --connect tcp://127.0.0.1:5564 --operation peek --limit 5 --format json
```

For native options, install `services/buffer/requirements.txt` and run
`python -m services.buffer --help`. Its fresh serving-loop snapshot lets monitor
read actual pending state even when empty. Unavailable/stale snapshots are unknown.

> [!IMPORTANT]
> Normal recreation preserves `buffer_data` and `delivery_row_builder`. Volume
> deletion or a new project namespace changes that behavior. Back up SQLite with
> its online API or stop its owner before copying the complete directory.

## Source map

| Location | Responsibility |
|---|---|
| [__main__.py](__main__.py) | Module entry point |
| [Dockerfile](Dockerfile) | Image layers, packaged code, entry point and healthcheck |
| [requirements.txt](requirements.txt) | Runtime dependencies |
| `messaging/` | Service-local wire contracts, validation and input/output adapters: [protocol.py](messaging/protocol.py), [publisher.py](messaging/publisher.py), [rows.py](messaging/rows.py) |
| `runtime/` | Configuration, CLI lifecycle, logging and independent healthcheck: [configuration.py](runtime/configuration.py), [healthcheck.py](runtime/healthcheck.py), [logging.py](runtime/logging.py), [service.py](runtime/service.py) |
| `storage/` | Owned persistence, transactional recovery and storage limits: [database.py](storage/database.py) |
| `tools/` | Diagnostic receivers and inspection utilities: [client.py](tools/client.py), [receiver.py](tools/receiver.py) |

## Operating notes

Run commands from the repository root. Use IDs from your installation; examples
use camera `entrance`, model `people` and device `node-01`. Host ports can change
with generated instance order; the umbrella endpoint maps are authoritative.

[Update only this image](../../docs/UPDATES.md) · [Inspect connections](../../docs/OBSERVABILITY.md) · [Storage and recovery](../../docs/STORAGE.md)
