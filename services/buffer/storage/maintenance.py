"""Bounded cleanup on its own connection; SQLite still serializes writers."""

import logging
import sqlite3
import threading

from .database import Database

log = logging.getLogger("buffer")


class Maintenance(threading.Thread):
    def __init__(self, path, max_size_mb, stop, interval=1):
        super().__init__(name="buffer-maintenance", daemon=True)
        self.path, self.max_size_mb, self.stop = path, max_size_mb, stop
        self.interval, self.error = interval, None

    def run(self):
        database = None
        try:
            database = Database(
                self.path, max_size_mb=self.max_size_mb, initialize=False
            )
            database.connection.execute("PRAGMA busy_timeout=20")
            while not self.stop.wait(self.interval):
                try:
                    database.maintain()
                except sqlite3.OperationalError as exc:
                    if getattr(exc, "sqlite_errorcode", None) not in (
                        sqlite3.SQLITE_BUSY,
                        sqlite3.SQLITE_LOCKED,
                    ):
                        raise
                    # Yield to serving transactions; try again next tick.
        except Exception as exc:
            self.error = exc
            log.exception("buffer maintenance failed")
        finally:
            if database:
                database.close()
