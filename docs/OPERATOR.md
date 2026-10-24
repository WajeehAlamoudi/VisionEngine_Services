# Operator menu

[Handbook](README.md) / Service controls

On a Linux edge device, run from the repository root:

```bash
bash scripts/service.sh
```

The menu groups configuration, image builds, container controls, live logs,
read-only diagnostics and Docker boot setup. It lists the actual service names
from generated Compose so you can choose all services or a single instance.
Colored headings appear on terminals; `NO_COLOR=1` disables them. Docker keeps
its native live build progress. Quiet dependency and Compose validation checks show
a loading spinner on interactive terminals; success/failure replaces that spinner.
Set `VISIONENGINE_NO_ANIMATION=1` to disable animation. Non-interactive output is
plain. Errors remain visible and return you to the menu.

## Menu actions

| Number | Action | Effect |
|---|---|---|
| 1 | Validate + generate | Validate source YAML and references, then write pipeline/Compose; starts nothing. |
| 2 | Build + deploy | Generate, validate, build selected images, recreate selected containers. |
| 3 | Build images only | Build images; running containers keep their current image. |
| 4 | Start existing images | Reconcile/start selected containers without building; applies Compose differences if needed. |
| 5 | Stop containers | Stop selected containers while preserving storage. |
| 6 | Restart containers | Restart current containers without rebuilding or applying new mounts/environment. |
| 7 | Container status | Show selected container state and health. |
| 8 | Live service logs | Follow input/process/output logs; Ctrl+C exits the view. |
| 9 | Diagnostics | Read-only host/configuration/deployment checks. |
| 10 | Enable Docker at boot | Enable the host Docker daemon; existing container policies control recovery. |
| 11 | Performance report | Read-only resource/sensor sample and container health assessment. |
| 0 | Exit | Close this menu; services keep running. |

Validation checks configuration, building creates an image, and deployment
creates/recreates a running container from that image. They are separate steps.

## Direct commands

```bash
# Read-only deployment checks: configuration, containers, health and restart policy.
bash scripts/service.sh diagnose

# Validate configuration and generate deployment files; start nothing.
bash scripts/service.sh generate

# Generate, build and recreate the whole deployment, then check its status.
bash scripts/service.sh deploy

# Update one service without touching upstream dependencies.
bash scripts/service.sh deploy visionengine-inference-service-people

# Build one image only; running containers keep their current image.
bash scripts/service.sh build visionengine-rules-service

# Start one existing image, then inspect it.
bash scripts/service.sh start visionengine-rules-service
bash scripts/service.sh logs visionengine-rules-service

# All containers in this Compose project, including stopped ones.
bash scripts/service.sh status

# Stop this deployment without deleting its data.
bash scripts/service.sh stop

# Resume with existing images.
bash scripts/service.sh start

# Enable Docker itself at boot; does not start stopped containers.
bash scripts/service.sh enable-boot
```

Use your generated service names. All operations use this repository's
`config/.generated/pipeline.compose.yaml`, regardless of the calling directory.
When `config/deployment.compose.yaml` exists, the Compose helper automatically
loads it after the generated pipeline for build/start/stop/status/log operations.
Generation leaves this local override intact. Direct Docker commands require
both files explicitly; see [GPU deployment](GPU_DEPLOYMENT.md).
Docker access uses your current account if possible, otherwise `sudo docker`.
Run the script as your normal user so generation uses your local Python setup
and files retain the correct ownership. Python is selected from `.venv/bin/python`
when available, otherwise `python3`; the controller requires PyYAML.

## What deploy does

1. Validate installation YAML and regenerate pipeline/Compose.
2. Validate Compose and the selected service name.
3. Build selected images with Docker's normal cache and visible progress.
4. Recreate selected containers using those images to reload configuration.
5. Show post-deployment container status. Run `diagnose` after health checks settle.

A failed generation, Compose validation or build prevents container recreation.
A recreation failure can leave a partial deployment; this is not an atomic release
or automatic rollback system. Recreating causes a brief service interruption.
Selected-service operations use `--no-deps`; upstream inputs must already run.
Full deployment removes orphan containers but never deletes volumes. Services
sharing one rebuilt image must all be recreated to apply that change; selecting
one camera does not update other camera containers automatically.

`restart` is only a process/container restart. Use `deploy` to apply changed image
code, mounts, environment or YAML. Model files must exist before recreation.

## Updates from Git

```bash
git pull --ff-only
# Apply required configuration/sample changes and supply new model assets first.
bash scripts/service.sh deploy
bash scripts/service.sh diagnose
```

Pulling source does not update ignored installation YAMLs automatically. Review
release/schema changes before applying them. This script does not pull Git,
install Docker/Python packages, copy samples, modify credentials or create a new
systemd service. See [selective updates](UPDATES.md) and [storage](STORAGE.md).

## Diagnostics and boot

Diagnostics show architecture, disk/memory, Docker boot/running state, source YAML
validation and all project containers. Per-container state, health and restart
policy are printed, with configured-service and running-container counts. Missing,
stopped, starting, unhealthy or healthcheck-less containers need attention and
produce a nonzero result. YAML validation does not prove model/hardware availability.
Diagnostics do not generate files, restart anything, probe the API/camera or print
full Compose configuration/environment values. Logs and receiver tools remain
necessary to verify exact detections, track IDs and backend delivery.

Boot enablement runs `systemctl enable docker`. Existing `unless-stopped`
containers resume when Docker starts, except containers manually stopped earlier.
No always-running controller is installed. Docker manages crash recovery;
unhealthy status alone does not cause a restart.

Ctrl+C leaves a live log view without stopping containers. The collector can stay
healthy after reaching its sample limit. Named volumes and collected datasets
survive ordinary stop/recreation; keep project/environment and storage names
stable. Starting ingest/notifier sends configured real backend requests and
notifications. No deletion/reset commands are provided by this script.

## Performance report

Choose **11** and then all services or a specific service. Direct equivalents:

```bash
bash scripts/service.sh report
bash scripts/service.sh report visionengine-inference-service-people

# Save the terminal report explicitly; tee creates the report file.
set -o pipefail
NO_COLOR=1 bash scripts/service.sh report | tee ~/visionengine-performance.txt
```

The report itself does not create files, generate config, restart containers,
change power modes or contact the camera/backend. Docker may request sudo access.
It prints UTC time, whole-host load/memory/disk, selected container state, health,
restart/OOM flags, and a Docker CPU/RAM/network/block-I/O/process sample. Missing,
stopped, starting, unhealthy, healthcheck-less or OOM-killed containers and failed
resource reads produce an attention result/nonzero exit. Historical restart counts
are displayed, not treated as a measured restart rate. A 100% Docker CPU reading
is one core; I/O values are cumulative counters rather than current bandwidth.

Temperature comes from readable Linux thermal zones. Readable hwmon power inputs
or averages are converted from microWatts to Watts and labelled by rail; overlapping
rails are never summed into a claimed board total. If installed, `tegrastats` is
sampled for three seconds on Jetson; otherwise NVIDIA GPU readings can come from
`nvidia-smi`. GPU watts are not board watts. Raspberry Pi `vcgencmd get_throttled`
flags are displayed when available; flags include current and historical events.
No tools are installed. Missing sensors/permissions are reported as unavailable,
not zero. True whole-device power may require an external meter.

A health snapshot pass only means the checked container conditions passed now.
It does not certify sufficient CPU/GPU headroom, thermal safety, target FPS, frame
drops, notification/ingest delivery, energy over time or sustained deployment
reliability. Use service logs for FPS, queue drops and latency, then measure a
real workload on the target device over time. Sensor output keeps its native
labels/units where a vendor utility is used.

Sensor references: [Linux hwmon](https://docs.kernel.org/hwmon/sysfs-interface.html),
[NVIDIA tegrastats](https://docs.nvidia.com/jetson/archives/r38.2/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/TegrastatsUtility.html).
