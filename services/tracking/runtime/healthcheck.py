"""Check tracking heartbeat and recent completed processing."""

import argparse
import json
from pathlib import Path
import time


def healthy(path, max_age=30):
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        now = time.time_ns()
        return (
            0 <= now / 1e9 - status["updated_at"] <= max_age
            and status["state"] in ("waiting", "tracking")
            and status["processed"] > 0
            and 0 <= (now - status["processed_at_ns"]) / 1e9 <= max_age
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    return 0 if healthy(args.file) else 1


if __name__ == "__main__":
    raise SystemExit(main())
