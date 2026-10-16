"""Read the main process heartbeat; unhealthy does not itself restart Docker."""

import argparse
import json
from pathlib import Path
import time


def healthy(path: Path, max_age=30.0) -> bool:
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        now_ns = time.time_ns()
        age = now_ns / 1_000_000_000 - status["updated_at"]
        return (
            0 <= age <= max_age
            and status["state"] in ("reading", "buffering", "pacing")
            and status["captured"] > 0
            and 0 <= (now_ns - status["received_at_ns"]) / 1_000_000_000 <= max_age
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--max-age", type=float, default=30)
    args = parser.parse_args()
    return 0 if healthy(args.file, args.max_age) else 1


if __name__ == "__main__":
    raise SystemExit(main())
