"""Monitor is healthy when it collects, even if monitored services are unhealthy."""

import argparse
import json
from pathlib import Path
import time


def healthy(path, max_age=30):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            data["state"] == "running"
            and data["processed"] > 0
            and 0 <= time.time() - data["updated_at"] <= max_age
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    return 0 if healthy(parser.parse_args().file) else 1


if __name__ == "__main__":
    raise SystemExit(main())
