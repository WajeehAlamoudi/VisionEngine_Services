# Observe the running pipeline

[Handbook](README.md) / Observability

## See the whole system

```console
docker compose -f config/.generated/pipeline.compose.yaml ps
docker compose -f config/.generated/pipeline.compose.yaml logs -f --tail 20 --no-log-prefix
docker stats --no-stream
```

Logs use UTC time, severity, instance, stage, message and labeled fields. `INPUT`
reports receiving/capture, `PROCESS` reports work, `OUTPUT` reports local publishing
or delivery, and `LIFECYCLE` reports state transitions. INFO provides periodic
counters; DEBUG provides finer activity. Log instance labels may be shorter than
Compose service names. Use the latter with Docker commands.

```text
21:14:35.272Z  INFO  camera-entrance   INPUT    capture status | camera=entrance captured=413 fps=12.30
21:14:36.594Z  INFO  inference-people  PROCESS  model status   | camera=entrance processed=137 fps=4.18
```

## Follow one connection

```console
docker compose -f config/.generated/pipeline.compose.yaml logs -f --tail 20 --no-log-prefix visionengine-inference-service-people visionengine-tracking-service-entrance visionengine-enrichment-service-entrance
```

For all cameras entering tracking, compare shared inference's per-camera PROCESS
rates with each tracker's INPUT/PROCESS rates. Low tracker FPS can reflect low
inference output. Compare queue depth, drops, inference duration and CPU before
identifying the bottleneck. Docker `100%` CPU is roughly one logical CPU; multicore
work can exceed it. Extra replicas compete for existing CPU/GPU and RAM. Automatic
inference replica load balancing is not implemented.

## Inspect messages inside containers

These diagnostic subscribers do not take messages away from production subscribers.
Examples assume camera `entrance`, model `people` and their generated service names.

```console
# Original frame: save inside the camera container.
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-camera-service-entrance python -m services.camera.tools.receiver --connect tcp://127.0.0.1:5555 --camera-id entrance --count 1 --save /tmp/camera-input.jpg

# Inference detections.
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-inference-service-people python -m services.inference.tools.receiver --connect tcp://127.0.0.1:5556 --camera-id entrance --count 5 --format json

# Tracking output.
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-tracking-service-entrance python -m services.tracking.tools.receiver --connect tcp://127.0.0.1:5558 --camera-id entrance --count 5 --format json

# Enrichment output.
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-enrichment-service-entrance python -m services.enrichment.tools.receiver --connect tcp://127.0.0.1:5559 --camera-id entrance --count 5 --format json

# Accepted events and alerts.
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-rules-service python -m services.rules.tools.receiver --connect tcp://127.0.0.1:5560 --camera-id entrance --kind accepted --count 5 --format json
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-rules-service python -m services.rules.tools.receiver --connect tcp://127.0.0.1:5561 --camera-id entrance --kind alerts --count 5 --format json

# Formatted database rows.
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-row-builder-service python -m services.row_builder.tools.receiver --connect tcp://127.0.0.1:5562 --camera-id entrance --count 10 --format json
```

`--count` counts batches, not objects. Empty batches are valid. To copy the saved
frame from the container onto your host:

```console
docker compose -f config/.generated/pipeline.compose.yaml cp visionengine-camera-service-entrance:/tmp/camera-input.jpg ./camera-input.jpg
```

Host-native receivers instead use the published **host** endpoint, such as
`127.0.0.1:6000` for the first model's detections, and require their service
requirements installed on the host. The generated endpoint maps are authoritative.

## Node rows and buffer backlog

Replace `node-01` with your configured device ID:

```console
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-row-builder-service python -m services.row_builder.tools.receiver --connect tcp://127.0.0.1:5562 --device-id node-01 --count 5 --format json
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-buffer-service python -m services.buffer.tools.client --connect tcp://127.0.0.1:5564 --operation stats --format json
docker compose -f config/.generated/pipeline.compose.yaml exec visionengine-buffer-service python -m services.buffer.tools.client --connect tcp://127.0.0.1:5564 --operation peek --limit 5 --format json
```

## Structured logs and health

PowerShell example:

```powershell
$env:VISIONENGINE_LOG_FORMAT = "json"
$env:VISIONENGINE_LOG_LEVEL = "INFO"
$env:VISIONENGINE_LOG_COLOR = "never"
docker compose -f config/.generated/pipeline.compose.yaml up --no-build -d
```

CMD uses `set VISIONENGINE_LOG_FORMAT=json`; POSIX uses
`export VISIONENGINE_LOG_FORMAT=json`. Return to console/always for colored text.
Environment changes need `up`/recreation, not merely `restart`. Native libraries
can emit their own diagnostics. Docker retains stdout/stderr according to its
logging driver and rotation settings; no separate application archive is created.

Unique snapshots under `/health` are runtime files on `health_snapshots`, not
files inside the image. Monitor reads them without Docker socket access and
publishes metadata; row builder builds node rows. `buffer_pending: null` means
unknown, not zero. Sampling is not packet tracing.

> [!NOTE]
> Published counters count local sends, not acknowledged receipt. Sequence gaps
> identify missing observations; local counters cannot measure every PUB/SUB loss.
> A health snapshot does not prove every connection delivered every message.
