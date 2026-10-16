"""Read buffer statistics or peek pending rows without reserving/delivering them."""

import argparse
import json

from ..messaging.protocol import Client
from ..runtime.logging import print_message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", required=True)
    parser.add_argument("--operation", choices=("stats", "peek"), default="stats")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--format", choices=("console", "json"), default="console")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args()
    client = Client(args.connect)
    try:
        result = client.request(
            args.operation,
            **({"limit": args.limit} if args.operation == "peek" else {}),
        )
        if args.format == "json":
            print(json.dumps(result), flush=True)
        else:
            print_message(
                {
                    "message_type": "buffer." + args.operation + ".v1",
                    "buffer": result
                    if args.operation == "stats"
                    else {"rows": len(result["rows"])},
                },
                "buffer",
                color=args.color,
            )
    except (ValueError, TimeoutError) as exc:
        parser.exit(1, str(exc) + "\n")
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
