"""Validate all configuration files and references without starting services."""

import argparse
from pathlib import Path

from .compiler import compile_pipeline
from .deployment import compile_deployment, dump_compose, split_compose
from .loader import load_file
from .loader import load_config
from .parsers.graph import parse as parse_graph
from .parsers.strict import ConfigError
from .writer import dump_yaml, write_yaml


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate registered configuration sections"
    )
    parser.add_argument("--config", type=Path, default=Path("config/YAMLs"))
    parser.add_argument(
        "--samples", action="store_true", help="read *.sample.yaml files"
    )
    parser.add_argument(
        "--details", action="store_true", help="show resolved camera/model settings"
    )
    parser.add_argument(
        "--plan", action="store_true", help="preview resolved nodes and edges as YAML"
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="write pipeline, service Compose files and umbrella Compose deployment",
    )
    parser.add_argument(
        "--compose-output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".generated" / "pipeline.compose.yaml",
        help="umbrella Compose destination (service files are written beside it)",
    )
    parser.add_argument(
        "--graph",
        type=Path,
        help="graph template (default: graph.yaml in --config directory)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".generated" / "pipeline.yaml",
        help="generated pipeline destination",
    )
    args = parser.parse_args()
    try:
        result = load_config(args.config, samples=args.samples)
        if args.plan or args.generate:
            graph_path = args.graph or args.config / (
                "graph.sample.yaml" if args.samples else "graph.yaml"
            )
            graph = parse_graph(load_file(graph_path, "graph"))
            pipeline = compile_pipeline(result, graph)
            pipeline["configuration_directory"] = str(args.config.resolve())
            pipeline["graph_definition"] = str(graph_path.resolve())
            deployment = (
                compile_deployment(result, pipeline, args.compose_output)
                if args.generate
                else None
            )
            if args.generate:
                umbrella, fragments = split_compose(deployment.compose)
                destinations = [
                    args.output.resolve(),
                    args.compose_output.resolve(),
                    *[
                        (args.compose_output.parent / name).resolve()
                        for name in fragments
                    ],
                ]
                if len(destinations) != len(set(destinations)):
                    raise ConfigError(
                        "deployment", ["generated artifact paths must be distinct"]
                    )
            if args.plan:
                print(dump_yaml(pipeline), end="")
            if args.generate:
                compose_changed = False
                for name, fragment in fragments.items():
                    compose_changed |= write_yaml(
                        args.compose_output.parent / name,
                        fragment,
                        formatter=dump_compose,
                    )
                compose_changed |= write_yaml(
                    args.compose_output, umbrella, formatter=dump_compose
                )
                changed = write_yaml(args.output, pipeline)
                print(
                    f"Pipeline {'written' if changed else 'unchanged'}: {args.output.resolve()}"
                )
                print(
                    f"{len(pipeline['nodes'])} nodes, {len(pipeline['edges'])} edges. No services started."
                )
                print(
                    f"Compose {'written' if compose_changed else 'unchanged'}: {args.compose_output.resolve()}"
                )
                metadata = deployment.compose["x-visionengine"]
                print(
                    f"Deployment: {len(metadata['frame_outputs'])} camera containers, {len(metadata['inference_outputs']) // 2} inference containers, {len(metadata['tracking_outputs'])} tracking containers, {len(metadata['enrichment_outputs'])} enrichment containers, {len(metadata['rules_outputs']) // 2} rules containers, {len(metadata['row_outputs'])} row builder containers, {len(metadata['buffer_outputs']) // 2} buffer containers, {len(metadata['ingest_outputs'])} ingest containers, {len(metadata['notifier_outputs'])} notifier containers, {len(metadata['collector_outputs'])} collector containers, {len(metadata['monitor_outputs'])} monitor containers; {len(metadata['omitted_nodes'])} other pipeline nodes not deployed."
                )
                for node, output in {
                    **metadata["frame_outputs"],
                    **metadata["inference_outputs"],
                    **metadata["tracking_outputs"],
                    **metadata["enrichment_outputs"],
                    **metadata["rules_outputs"],
                    **metadata["row_outputs"],
                    **metadata["monitor_outputs"],
                    **metadata["buffer_outputs"],
                    **metadata["ingest_outputs"],
                    **metadata["notifier_outputs"],
                    **metadata["collector_outputs"],
                }.items():
                    print(
                        f"  {node}: {output['host_connect']} (Compose network: {output['connect']})"
                    )
                print(
                    "Docker has not been run. Hardware and source-file availability are not checked."
                )
            return 0
    except ConfigError as exc:
        parser.exit(1, f"{exc}\n")
    except OSError:
        parser.exit(
            1,
            "Could not write generated artifacts. Check the output paths and permissions.\n",
        )
    cameras = result["cameras"]
    print("Configuration valid: all 8 files and cross-file references checked.")
    print(
        f"Cameras: {len(cameras)} defined, {sum(c.enabled for c in cameras)} enabled."
    )
    print(f"Models: {len(result['models'])}; rules: {len(result['rules'])}.")
    print(f"Tracker: {result['tracker'].algorithm}.")
    if args.details:
        print("Resolved camera settings (not a generated execution graph):")
        for camera in cameras:
            model = result["models"][camera.model_id]
            status = "enabled" if camera.enabled else "disabled"
            tracking = "enabled" if model.use_tracker else "disabled"
            print(f"  {camera.id}: {status}")
            print(f"    model: {model.id} ({model.runtime}, {model.accelerator})")
            print(f"    classes: {', '.join(camera.classes)}")
            print(f"    fps_target: {camera.fps_target}; tracking: {tracking}")
    print(
        "Hardware/model-file availability is not checked. No services started or graph generated."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
