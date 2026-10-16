# Deploy a device

[Handbook](README.md) / Deployment

## 1 · Prepare the host

Use Docker Engine on Linux, or Docker Desktop with Linux containers on Windows.
Use Compose v2.20.0 or newer for `include`. Python 3.12 is the reference runtime.
Check tools in the same terminal used for deployment:

```console
python --version
docker version
docker compose version
```

Docker packages user-space dependencies. Architecture, RAM, host drivers and GPU
runtime still matter. Reference inference/tracking images use CPU Torch. Jetson
requires a compatible JetPack/vendor image and GPU access; native Apple backends
are not provided inside Linux Docker containers.

## 2 · Clone and configure

```console
git clone https://github.com/WajeehAlamoudi/VisionEngine_Services.git
cd VisionEngine_Services
python -m venv .venv
```

Activate the environment for your shell, then:

```console
python -m pip install -r config/controller/requirements.txt
```

[Copy the nine configuration samples](../config/README.md#prepare-installation-files).
Supply camera sources, local model weights, coherent rules and device/backend
identity. Installation files, credentials and weights are ignored by Git; a fresh
clone does not include your device's values.

## 3 · Validate and generate

```console
python -m config.controller --details
python -m config.controller --generate
```

The controller parses and validates configuration, resolves enabled graph nodes
and connections, then writes the pipeline and modular Compose files. `--plan`
previews the resolved graph without writing. Generation does not run Docker,
open cameras or verify hardware/model-file availability.

The umbrella includes service Compose files into one application. Relative paths
are resolved from each included file's directory; keep generated files together.
[Docker include reference](https://docs.docker.com/reference/compose-file/include/)

## 4 · Review before starting

```console
docker compose -f config/.generated/pipeline.compose.yaml config --services
```

Read `pipeline.yaml` for logical nodes/edges. Read `pipeline.compose.yaml` for the
project, endpoint maps, shared volumes and includes. `x-visionengine` is descriptive
metadata; Docker ignores it. Actual service commands establish connections.
Enabled SMTP channels require their password environment variables before Compose
interpolation. Set them in the shell or supply an explicit private `--env-file`.

> [!CAUTION]
> Starting ingest can send queued rows to the configured backend. Enabled notifier
> channels can send real webhooks/email. Review addresses, credentials, schedules
> and cooldowns first.

## 5 · Run and verify

```console
docker compose -f config/.generated/pipeline.compose.yaml up --build -d --remove-orphans
docker compose -f config/.generated/pipeline.compose.yaml ps
docker compose -f config/.generated/pipeline.compose.yaml logs -f --tail 20 --no-log-prefix
```

Confirm cameras reach reading state, inference completes work, downstream counters
progress, and buffer/ingest show delivery activity. Empty object batches are valid.
[Inspect stage outputs](OBSERVABILITY.md) when expected data disappears.
Host outputs bind to `127.0.0.1` by default.

Instances follow configuration: cameras/tracking/enrichment per applicable camera,
inference per used model, collectors per enabled session, and shared stages when
selected by the graph. Container count is not fixed.

## Stop, resume and recover

```console
# Stop; retain containers and data.
docker compose -f config/.generated/pipeline.compose.yaml stop

# Start/reconcile using existing images.
docker compose -f config/.generated/pipeline.compose.yaml up --no-build -d

# Remove containers/network; retain named volumes by default.
docker compose -f config/.generated/pipeline.compose.yaml down
```

Services use `init: true` and `restart: unless-stopped`. Docker restarts exited
processes while its daemon is running. Unhealthy status alone does not cause a
restart; service watchdogs exit on certain stuck operations. Deliberately stopped
containers stay stopped. Automatic boot recovery also requires Docker daemon
startup; Docker Desktop has its own startup/login behavior. The controller is a
local command, not a continuously running orchestrator.

Read [updates](UPDATES.md) before changing code/configuration and
[storage](STORAGE.md) before renaming a project or changing volumes.
