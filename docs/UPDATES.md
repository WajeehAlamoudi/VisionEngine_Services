# Update code without rebuilding everything

[Handbook](README.md) / Updates

Service code is copied into images. A host edit needs an image rebuild and
container recreation. Normal recreation preserves mounted volumes.
[Docker update behavior](https://docs.docker.com/reference/cli/docker/compose/up/)

## Choose the smallest update

| Change | Generate? | Build? | Apply |
|---|---|---|---|
| One service's Python code | Usually no | That image | Recreate all affected instances |
| Requirements / Dockerfile | Usually no | That image | Recreate affected instances |
| Shared controller/parser code | Yes | Every image needing the changed parser | Recreate affected instances |
| Enabled nodes / graph / ports / assignments | Yes | If code changed or images are missing | Reconcile with `up --no-build -d --remove-orphans` |
| Thresholds / schedules / rules | Yes, validate and capture topology changes | No | Force-recreate affected services to reload YAML |
| Logging / SMTP environment | If channels or secret names change | No | Use `up`; `restart` does not apply new Compose environment |
| Model weights at the same path | If settings change | No | Recreate relevant inference instances |

## Camera code: build once, recreate all affected cameras

```console
docker compose -f config/.generated/pipeline.compose.yaml build visionengine-camera-service-entrance
docker compose -f config/.generated/pipeline.compose.yaml up --no-build --no-deps --force-recreate -d visionengine-camera-service-entrance visionengine-camera-service-parking
```

Every camera uses `visionengine-camera:local`. The example recreates two cameras;
list **all configured camera instances** affected by a shared-code update. Building
through one instance does not replace other running containers. `--no-deps` leaves
upstream services alone. [Docker selective updates](https://docs.docker.com/compose/how-tos/production/)

## One shared service

```console
docker compose -f config/.generated/pipeline.compose.yaml build visionengine-rules-service
docker compose -f config/.generated/pipeline.compose.yaml up --no-build --no-deps --force-recreate -d visionengine-rules-service
docker compose -f config/.generated/pipeline.compose.yaml logs -f --tail 20 visionengine-rules-service
```

## Configuration only

```console
python -m config.controller --details
python -m config.controller --generate
docker compose -f config/.generated/pipeline.compose.yaml up --no-build -d --remove-orphans
```

If only bind-mounted YAML contents changed, Compose may see no change to its own
service definition. Explicitly recreate the affected process to reload it:

```console
docker compose -f config/.generated/pipeline.compose.yaml up --no-build --no-deps --force-recreate -d visionengine-rules-service
```

Use the appropriate service names. Camera/model/device settings can affect many
services. Parser and contract changes can require a coordinated image release.

## Why first builds take longer

Torch, BoxMOT and OpenCV create large dependency layers. Dockerfiles copy
requirements before code so unchanged dependencies can be cached. Code-only
rebuilds normally reuse them. First builds, changed requirements/base images and
missing caches can download dependencies again. Routine updates do not need
`--no-cache` or broad cache pruning.

> [!IMPORTANT]
> Keep environment, volume names and storage paths stable during routine updates.
> `down -v` deletes named volumes; it is not an update step. Host dataset directories
> must also be preserved. [Storage and backup](STORAGE.md)

Record the source revision, image IDs/tags, configuration and volume names before
an update. The mutable `:local` tag is not a rollback archive. Storage/schema
changes need a compatible backup and migration plan.
