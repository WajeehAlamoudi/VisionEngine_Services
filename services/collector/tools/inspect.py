"""Read-only inspection of committed dataset samples and optional file verification."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def inspect(root, session_id, limit=5, verify=False):
    root = Path(root).resolve()
    path = root / session_id
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id) or path.resolve().parent != root:
        raise ValueError("invalid session directory")
    database = path / ".collector.sqlite3"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        counts = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT state,COUNT(*) FROM samples GROUP BY state"
            )
        }
        samples = [
            {
                "id": r["id"],
                "capture_ns": r["capture_ns"],
                "image": f"images/{r['id']}.jpg",
                "label": f"labels/{r['id']}.txt",
            }
            for r in connection.execute(
                "SELECT id,capture_ns FROM samples WHERE state='committed' ORDER BY capture_ns DESC LIMIT ?",
                (limit,),
            )
        ]
        result = {"session_id": session_id, "counts": counts, "samples": samples}
        if verify:
            checked = missing = modified = 0
            for row in connection.execute(
                "SELECT id,files FROM samples WHERE state='committed'"
            ):
                if not re.fullmatch(r"[0-9a-f]{64}", row["id"]):
                    raise ValueError("invalid stored identity")
                for kind, expected in json.loads(row["files"]).items():
                    folder, suffix = {
                        "image": ("images", "jpg"),
                        "label": ("labels", "txt"),
                        "metadata": ("metadata", "json"),
                    }[kind]
                    file = path / folder / f"{row['id']}.{suffix}"
                    if file.is_symlink() or not file.is_file():
                        missing += 1
                        continue
                    checked += 1
                    if hashlib.sha256(file.read_bytes()).hexdigest() != expected:
                        modified += 1
            result["verification"] = {
                "checked_files": checked,
                "missing_files": missing,
                "modified_files": modified,
            }
        return result
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("collected"))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("limit must be 1..1000")
    try:
        result = inspect(args.output_dir, args.session_id, args.limit, args.verify)
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(1, "Dataset is missing, invalid or unavailable.\n")
    print(json.dumps(result, indent=2))
    return int(
        any(
            result.get("verification", {}).get(k, 0)
            for k in ("missing_files", "modified_files")
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
