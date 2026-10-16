# Naming conventions

[Handbook](README.md) / Naming

Configuration IDs identify the data. Compose names identify the containers
running those identities. Generated filenames organize deployment definitions.

## The four Docker names

| Name | Rule | Example |
|---|---|---|
| Project | `visionengine-<environment>-edge` | `visionengine-development-edge` |
| Image | `visionengine-<service>:local` | `visionengine-camera:local` |
| Compose service | `visionengine-<service>-service[-<instance>]` | `visionengine-camera-service-entrance` |
| Container | Docker combines project, service and replica | `visionengine-development-edge-visionengine-camera-service-entrance-1` |

The environment comes from `device.environment` in `config/YAMLs/device.yaml`:

```yaml
device:
  # Excerpt from the complete device configuration.
  environment: development  # development | production
```

Production becomes `visionengine-production-edge`. Device ID is not part of the
project name. Avoid overriding it with `-p` or `COMPOSE_PROJECT_NAME` unless you
intend to select another deployment namespace.

## Service names

| Service | Instance scope | Example |
|---|---|---|
| Camera | Camera ID | `visionengine-camera-service-entrance` |
| Inference | Model ID | `visionengine-inference-service-people` |
| Tracking | Camera ID | `visionengine-tracking-service-entrance` |
| Enrichment | Camera ID | `visionengine-enrichment-service-entrance` |
| Collector | Session ID | `visionengine-collector-service-entrance-dataset` |
| Rules | Shared | `visionengine-rules-service` |
| Row builder | Shared | `visionengine-row-builder-service` |
| Buffer | Shared | `visionengine-buffer-service` |
| Ingest | Shared | `visionengine-ingest-service` |
| Notifier | Shared | `visionengine-notifier-service` |
| Monitor | Shared | `visionengine-monitor-service` |

The controller normalizes graph-node slashes/underscores to lowercase hyphens.
`entrance_dataset` remains unchanged inside configuration/messages, while the
Compose suffix becomes `entrance-dataset`. Normalized name collisions are rejected.
Runtime log labels may be shorter than Compose service names.

## Generated names

| Artifact | Role |
|---|---|
| `pipeline.yaml` | Resolved logical nodes, ports, connections and policies |
| `pipeline.compose.yaml` | Umbrella containing project, metadata, volumes and includes |
| `<service>.compose.yaml` | All deployed containers of one service type |

All files live directly in `config/.generated/`. `row_builder.compose.yaml`
follows the source directory spelling; Docker names use `row-builder`. Only active
service types are included. Edit installation YAMLs, not generated outputs.

## Critical notice: names and persisted data

> [!IMPORTANT]
> Docker's default named volumes are project-scoped. Changing development to
> production selects different volumes. The old volumes remain, but their data
> does not automatically appear in the new project.

For example, `visionengine-development-edge_buffer_data` becomes
`visionengine-production-edge_buffer_data`. The producer journal follows the
same rule with `delivery_row_builder`.

Before changing environment, stop the old deployment using its existing generated
files, record volume names and back up durable state. Plan a deliberate migration
before starting the new namespace. Never remove volumes to perform a naming change.
Two checkouts with the same project name on one Docker daemon can target the same
deployment. Dataset bind mounts use the configured host directory and are not
renamed by a Compose project change.
