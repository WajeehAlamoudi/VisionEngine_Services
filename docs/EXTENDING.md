# Extend the graph

[Handbook](README.md) / Extension guide

A service needs a contract, executable implementation, graph integration and
deployment support. Adding a YAML file alone does not make it executable.

| Concern | Owner |
|---|---|
| Installation intent | `config/YAMLs/`; examples in `config/config_sample/` |
| Loading and parsing | `config/controller/loader.py`, `parsers/`, `registry.py` |
| Cross-file checks | `config/controller/validation.py` |
| Graph expansion and contracts | `config/controller/compiler.py`, graph parser/template |
| Containers, mounts and endpoints | `config/controller/deployment.py` |
| YAML writing | `config/controller/writer.py` and CLI |
| Lifecycle and domain behavior | `services/<name>/runtime/` and processing modules |
| Wire contracts/adapters | Service-local `messaging/` |

## Add a stage

1. Define inputs, outputs, versions, identity keys and limits. Decide whether it
   needs pixels, metadata or both.
2. Add the service entry point, runtime, messaging, domain logic, Dockerfile,
   requirements and README. Output adapters use `publisher.py`.
3. Add a parser/registration only if new settings are needed. Extend cross-file
   checks; reuse camera/model/device context when sufficient.
4. Define graph nodes, ports, conditions, edges and instance expansion.
5. Add its deployment builder and endpoint maps. The writer groups by service
   Dockerfile into `<service>.compose.yaml` and includes it in the umbrella.
6. Define health evidence, monitor wiring, bounded queues and loss counters.
   Define the persistence boundary before claiming durable delivery.
7. Generate, inspect connections, build its image and verify real output on the
   intended device with a diagnostic receiver.

Disabled/absent nodes do not run. The graph template declares connections and
conditions; `pipeline.yaml` records resolved nodes/edges. Nodes without executable
deployment support appear in `omitted_nodes`. A drawn graph is not deployment proof.

## Keep frontend streaming separate

A future gateway can consume annotated inference frames and encode a browser
protocol. It needs its own adapter, lifecycle, configuration and generator support.
Raw `frame.v1` over ZeroMQ is not HTTP, RTSP or WebRTC.

## Remote execution needs explicit deployment work

Services accept explicit reachable TCP endpoints. Remote execution also needs the
image, installation configuration/assets, published ports and secure routing.
The current generator does not partition hosts, allocate remote endpoints or
replace monitor's local shared files with network telemetry. Those are separate
features rather than Dockerfile-only changes.
