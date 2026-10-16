# Troubleshooting

[Handbook](README.md) / Troubleshooting

| Symptom | Check | Action |
|---|---|---|
| `docker` not recognized | Newly opened terminal | Install/start Docker; reopen terminal/IDE to refresh PATH |
| Docker daemon unavailable | Server section of `docker version` | Start daemon/Desktop; use Linux containers |
| Unsupported `include` | `docker compose version` | Use v2.20.0+ |
| Samples rejected | Validation file/path | Fill placeholders and reconcile camera/model/rule references |
| Missing model | Asset path and generated mount | Supply weights and compatible backend dependencies |
| USB camera unavailable | Host access/container devices | Linux needs passthrough; Desktop Windows webcams are not automatic |
| Reconnecting camera | Camera logs and source reachability | Check credentials, URL and network from container |
| Receiver silent | Topic ID and endpoint | Verify publisher activity; connect before sending; distinguish host/container ports |
| Detections but empty rows | Rules input/accepted/alert counters | Inspect accepted events separately from alerts; check route/table |
| Alerts empty | Camera/class/zone/schedule/cooldown | `notify` alone does not bypass rule matching |
| No notification rows | Rule's `notifications_table` | Null can permit live alerts while suppressing notification rows |
| Slow inference | PROCESS FPS, CPU, queue drops | Measure actual model/hardware; tune workload rather than only queue size |
| Collector idle | Exact matches, UTC window, filters and cap | Check cache sizes and completed state |
| Collector storage error | Space, permissions, class-map journal | Restore access or use a new session for changed class maps |
| SMTP startup failure | Password environment variable | Set before Compose reads files; verify provider/TLS settings |
| Growing buffer | Ingest status, backend auth/reachability | Confirm successful backend response and retained pending rows |
| Empty buffer after rename | Environment/project and volumes | Locate old namespace; migrate deliberately |

## Three endpoint rules

1. Within Compose, use service DNS, such as
   `tcp://visionengine-camera-service-entrance:5555`.
2. From the host, use the published host port, such as `tcp://127.0.0.1:5555`.
3. Between devices, use a reachable host address and published port. Localhost and
   Compose DNS do not cross machines. Configure routing/firewall/private access;
   the raw TCP adapter does not add encryption or authentication.

## Restart is not a rebuild

A restart reruns existing software. Host code copied into an image requires a
rebuild and recreation. Mounted YAML changes need a process reload; graph changes
also need regeneration. [Selective updates](UPDATES.md)

## Find the earliest missing output

Follow camera → inference → tracking → enrichment → rules → rows using the
[inspection guide](OBSERVABILITY.md). Trackers can suppress tentative detections.
Accepted events can exist without alerts. `alert.v1` with `alerts: []` is a valid
empty batch, not a row-builder failure.

A receiver's `KeyboardInterrupt` after Ctrl+C means the diagnostic process was
interrupted. That traceback alone does not mean the publisher failed. Check
`compose ps` and actual service logs separately.

## Keep limitations visible

GPU selectors do not install drivers/wheels. Extra replicas do not create compute
capacity, and naive PUB/SUB replication can duplicate work. Trackers need ordered,
camera-specific state. Docker restarts on exit, not just an unhealthy flag.
Automatic multi-host placement and frontend RTSP/WebRTC publishing remain outside
the current generator.
