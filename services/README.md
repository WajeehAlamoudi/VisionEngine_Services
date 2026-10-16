<div align="center">

![Vision Engine Services](../docs/assets/services.svg)

**Independent Docker services · Explicit contracts · Configuration-driven connections**

[Project](../README.md) · [Configuration](../config/README.md) · [Architecture](../docs/ARCHITECTURE.md) · [Operating guides](../docs/README.md)

</div>

## Service catalog

| Service | Input → output | Scope | Runtime stack |
|---|---|---|---|
| [Camera](camera/README.md) | Source → original frames | Camera | OpenCV / ZeroMQ |
| [Inference](inference/README.md) | Frames → detections + annotated frames | Model | Ultralytics / Torch |
| [Tracking](tracking/README.md) | Detections + optional pixels → tracks | Camera | BoxMOT |
| [Enrichment](enrichment/README.md) | Detections → geometry/context events | Camera | Python / ZeroMQ |
| [Rules](rules/README.md) | Enriched events → accepted events + alerts | Shared | Python / UTC schedules |
| [Row builder](row_builder/README.md) | Events / alerts / health → table rows | Shared | Python / producer SQLite journal |
| [Buffer](buffer/README.md) | Durable puts → leased rows | Shared | SQLite WAL / ZeroMQ RPC |
| [Ingest](ingest/README.md) | Leased rows → backend HTTP | Shared | Python / HTTP |
| [Notifier](notifier/README.md) | Fired alerts → webhook / SMTP / logs | Shared | Async HTTP / aiosmtplib |
| [Monitor](monitor/README.md) | Health snapshots → device metadata | Device | Python / shared snapshots |
| [Collector](collector/README.md) | Original frames + detections → dataset | Collection session | OpenCV / journaled files |

## Common structure

Each service owns its entry point, Dockerfile, requirements, runtime and adapters.
`messaging/` contains local contracts and publisher/subscriber code. `runtime/`
handles configuration, lifecycle, health and logs. Domain logic lives in
`processing/`, `capture/` or `storage/` as appropriate. `tools/` provides diagnostic
receivers/inspection. The configuration controller is copied into images for
parsing; services do not import sibling implementations or the parent Edge project.

Container instances are generated from installation YAMLs and the graph. Files
under `config/.generated/` are deployment output, not per-service configuration
copies. Use the umbrella `pipeline.compose.yaml` for the whole deployment.
[Naming](../docs/NAMING.md) · [Selective image updates](../docs/UPDATES.md)

## Boundaries to preserve

Enrichment adds geometry; rules decides zones/matches/cooldowns. Row builder maps
to database schemas; buffer owns durable row storage; ingest owns backend HTTP.
Notifier live delivery is independent of notification-table rows. Collector uses
original pixels and direct model detections, not drawn previews. Monitor measures
and publishes health; it does not build database rows.

Live transport is best effort. Durable local row delivery begins at the producer
journal. Backends still need idempotency for duplicate-free retries. A browser
streaming gateway and automatic multi-host deployment are future work.
