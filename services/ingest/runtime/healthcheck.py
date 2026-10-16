"""Worker liveness is independent of backend availability."""

import argparse
import json
from pathlib import Path
import time


def healthy(path, max_age=30):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            data["state"]
            in ("waiting", "flushing", "posting", "ack_pending", "backoff")
            and 0 <= time.time() - data["updated_at"] <= max_age
            and 0
            <= (time.time_ns() - data["heartbeat_at_ns"]) / 1e9
            <= data.get("max_work_age_seconds", max_age)
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    return 0 if healthy(parser.parse_args().file) else 1


if __name__ == "__main__":
    raise SystemExit(main())
