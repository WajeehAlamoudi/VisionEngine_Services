"""Single-owner dataset journal and recoverable clean-image/YOLO-label publication."""

from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

import cv2
import yaml


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def durable_write(path, payload):
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path):
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Dataset:
    def __init__(
        self,
        root,
        session_id,
        camera_id,
        class_names,
        *,
        metadata=False,
        min_free_bytes=268435456,
    ):
        self.connection = self.lock_file = None
        self.root = Path(root).resolve()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise ValueError("unsafe session directory")
        if (self.root / ".git").exists() or (self.root / "config/controller").is_dir():
            raise ValueError("output root must be a dedicated dataset directory")
        self.path = self.root / session_id
        if self.path.resolve().parent != self.root:
            raise ValueError("session directory escapes output root")
        if not class_names or len(set(class_names)) != len(class_names):
            raise ValueError("dataset requires a unique nonempty class list")
        self.names = list(class_names)
        self.class_ids = {name: i for i, name in enumerate(self.names)}
        self.camera_id, self.metadata, self.min_free_bytes = (
            camera_id,
            metadata,
            min_free_bytes,
        )
        self.recovered_total = self.quarantined_total = 0
        self.total = 0
        self.initialized = False
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            for name in ("images", "labels", "metadata", ".pending", ".quarantine"):
                child = self.path / name
                if child.is_symlink():
                    raise ValueError("dataset subdirectories must not be symlinks")
                child.mkdir(exist_ok=True)
            # Also protect a custom collection output root from accidental Git adds.
            with suppress(FileExistsError):
                with (self.root / ".gitignore").open("x", encoding="utf-8") as stream:
                    stream.write(
                        "# Runtime datasets are local to this installation.\n*\n"
                    )
            if any(
                (self.path / name).is_symlink()
                for name in (".collector.lock", ".collector.sqlite3")
            ):
                raise ValueError("dataset state files must not be symlinks")
            self.lock_file = (self.path / ".collector.lock").open("a+b")
            if self.lock_file.tell() == 0:
                self.lock_file.write(b"0")
                self.lock_file.flush()
            self.lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.connection = sqlite3.connect(
                self.path / ".collector.sqlite3", timeout=5
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            with self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY,state TEXT NOT NULL,capture_ns INTEGER NOT NULL,files TEXT NOT NULL)"
                )
                self.connection.execute(
                    "CREATE INDEX IF NOT EXISTS sample_state ON samples(state)"
                )
                expected = {"schema": 1, "camera_id": camera_id, "names": self.names}
                existing = self.connection.execute(
                    "SELECT value FROM meta WHERE key='dataset'"
                ).fetchone()
                if existing and json.loads(existing[0]) != expected:
                    raise ValueError(
                        "existing dataset camera/class mapping differs; use a new session id"
                    )
                if existing is None and (
                    any((self.path / "images").iterdir())
                    or any((self.path / "labels").iterdir())
                ):
                    raise ValueError(
                        "existing dataset has no collector journal; use a new session id or migrate it explicitly"
                    )
                if existing is None:
                    self.connection.execute(
                        "INSERT INTO meta VALUES('dataset',?)", (json.dumps(expected),)
                    )
            descriptor = self.path / "data.yaml"
            if descriptor.exists():
                if descriptor.stat().st_size > 65536:
                    raise ValueError("dataset descriptor exceeds limit")
                current = yaml.safe_load(descriptor.read_text(encoding="utf-8"))
                names = current.get("names") if isinstance(current, dict) else None
                if isinstance(names, dict):
                    if set(names) != set(range(len(self.names))):
                        raise ValueError("existing dataset class ids differ")
                    names = [names[i] for i in range(len(self.names))]
                if names != self.names:
                    raise ValueError(
                        "existing data.yaml class mapping differs; use a new session id"
                    )
            else:
                temporary = self.path / ".descriptor.tmp"
                temporary.unlink(missing_ok=True)
                content = yaml.safe_dump(
                    {
                        "train": "images",
                        "val": "images",
                        "nc": len(self.names),
                        "names": dict(enumerate(self.names)),
                    },
                    sort_keys=False,
                ).encode()
                durable_write(temporary, content)
                os.replace(temporary, descriptor)
                sync_directory(self.path)
            self.recover()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def final_path(self, identity, kind):
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("invalid sample identity in journal")
        folder, extension = {
            "image": ("images", "jpg"),
            "label": ("labels", "txt"),
            "metadata": ("metadata", "json"),
        }[kind]
        path = self.path / folder / f"{identity}.{extension}"
        if path.is_symlink():
            raise ValueError("dataset files must not be symlinks")
        return path

    def staged_path(self, identity, kind):
        stage = self.path / ".pending" / identity
        if stage.is_symlink():
            raise ValueError("pending sample directories must not be symlinks")
        return stage / kind

    def quarantine(self, identity, files):
        destination = self.path / ".quarantine" / identity
        destination.mkdir(exist_ok=True)
        for kind in files:
            for prefix, path in (
                ("final", self.final_path(identity, kind)),
                ("staged", self.staged_path(identity, kind)),
            ):
                if path.exists():
                    target = destination / f"{prefix}-{kind}"
                    if target.exists():
                        raise OSError(
                            "quarantine conflict; preserve data for inspection"
                        )
                    os.replace(path, target)
        with self.connection:
            self.connection.execute(
                "UPDATE samples SET state='aborted' WHERE id=?", (identity,)
            )
        stage = self.path / ".pending" / identity
        if stage.exists():
            stage.rmdir()
        self.quarantined_total += 1

    def publish(self, identity, files):
        # Publish the image last. The journal is the completion authority across multiple files.
        for kind in ("label", "metadata", "image"):
            if kind not in files:
                continue
            final = self.final_path(identity, kind)
            staged = self.staged_path(identity, kind)
            if final.exists():
                if digest(final.read_bytes()) != files[kind]:
                    raise OSError(
                        "existing sample conflicts with pending dataset transaction"
                    )
                if staged.exists():
                    staged.unlink()
                continue
            os.replace(staged, final)
            sync_directory(final.parent)
        with self.connection:
            self.connection.execute(
                "UPDATE samples SET state='committed' WHERE id=?", (identity,)
            )
        self.total += 1
        (self.path / ".pending" / identity).rmdir()

    def recover(self):
        changed = False
        for row in self.connection.execute(
            "SELECT * FROM samples WHERE state='pending'"
        ).fetchall():
            changed = True
            files = json.loads(row["files"])
            valid = True
            for kind, expected in files.items():
                paths = (
                    self.final_path(row["id"], kind),
                    self.staged_path(row["id"], kind),
                )
                existing = [p for p in paths if p.exists()]
                if not existing or any(
                    digest(p.read_bytes()) != expected for p in existing
                ):
                    valid = False
                    break
            if valid:
                self.publish(row["id"], files)
                self.recovered_total += 1
            else:
                self.quarantine(row["id"], files)
        for stage in (self.path / ".pending").iterdir():
            if (
                not stage.is_dir()
                or stage.is_symlink()
                or not re.fullmatch(r"[0-9a-f]{64}", stage.name)
            ):
                raise ValueError("unexpected pending dataset entry")
            row = self.connection.execute(
                "SELECT state FROM samples WHERE id=?", (stage.name,)
            ).fetchone()
            if row and row[0] == "pending":
                continue
            for item in stage.iterdir():
                if (
                    item.name not in ("image", "label", "metadata")
                    or not item.is_file()
                    or item.is_symlink()
                ):
                    raise ValueError(
                        "unexpected staging file; preserved for inspection"
                    )
                # Unjournaled files never became visible images/labels.
                destination = self.path / ".quarantine" / stage.name
                destination.mkdir(exist_ok=True)
                target = destination / f"orphan-{item.name}"
                if target.exists():
                    raise OSError("orphan quarantine conflict")
                os.replace(item, target)
            stage.rmdir()
        if changed or not self.initialized:
            self.total = self.connection.execute(
                "SELECT COUNT(*) FROM samples WHERE state='committed'"
            ).fetchone()[0]
            self.initialized = True

    def save(self, header, image, result, detections, *, max_frames=0):
        self.recover()
        if max_frames and self.total >= max_frames:
            return False
        key = [header["camera_id"], header["stream_id"], header["sequence"]]
        identity = digest(json.dumps(key, separators=(",", ":")).encode())
        existing = self.connection.execute(
            "SELECT state,capture_ns FROM samples WHERE id=?", (identity,)
        ).fetchone()
        if existing:
            if existing["capture_ns"] != header["received_at_ns"]:
                raise ValueError("sample identity has a conflicting capture timestamp")
            return False
        h, w = image.shape[:2]
        lines = []
        for det in detections:
            if det["class_name"] not in self.class_ids:
                raise ValueError("detection class is absent from the dataset class map")
            x1, y1, x2, y2 = det["bbox"]
            if x2 <= x1 or y2 <= y1:
                raise ValueError("zero-area annotation cannot be saved")
            lines.append(
                f"{self.class_ids[det['class_name']]} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} {(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}"
            )
        success, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not success:
            raise OSError("JPEG encoding failed")
        payloads = {
            "image": encoded.tobytes(),
            "label": ("\n".join(lines) + ("\n" if lines else "")).encode(),
        }
        if self.metadata:
            payloads["metadata"] = json.dumps(
                {
                    "message_type": "dataset.sample.v1",
                    "identity": identity,
                    "session_id": self.path.name,
                    "camera_id": header["camera_id"],
                    "stream_id": header["stream_id"],
                    "sequence": header["sequence"],
                    "received_at_ns": header["received_at_ns"],
                    "width": w,
                    "height": h,
                    "model_id": result["model_id"],
                    "model_version": result["model_version"],
                    "detections": detections,
                },
                allow_nan=False,
                indent=2,
            ).encode()
        if shutil.disk_usage(self.path).free < self.min_free_bytes + sum(
            map(len, payloads.values())
        ):
            raise OSError("collection storage reserve reached")
        stage = self.path / ".pending" / identity
        stage.mkdir()
        for kind, payload in payloads.items():
            durable_write(stage / kind, payload)
        sync_directory(stage)
        files = {kind: digest(payload) for kind, payload in payloads.items()}
        with self.connection:
            self.connection.execute(
                "INSERT INTO samples VALUES(?,'pending',?,?)",
                (identity, header["received_at_ns"], json.dumps(files)),
            )
        self.publish(identity, files)
        return True
