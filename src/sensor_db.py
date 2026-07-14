"""SQLite persistence — temperature readings and control actions."""
from __future__ import annotations

import sqlite3
import logging
from pathlib import Path

_LOGGER = logging.getLogger("SensorDB")


class SensorDB:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS temperature_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT DEFAULT (datetime('now')),
                htemp_c    REAL,
                target_c   REAL,
                otemp_c    REAL
            );
            CREATE TABLE IF NOT EXISTS control_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT DEFAULT (datetime('now')),
                htemp_c    REAL,
                target_c   REAL,
                mode       TEXT,
                fan_speed  TEXT
            );
        """)
        self.conn.commit()

    def log_temperature(self, htemp: float, target: float, otemp: float = 0.0):
        try:
            self.conn.execute(
                "INSERT INTO temperature_log (htemp_c, target_c, otemp_c) VALUES (?, ?, ?)",
                (htemp, target, otemp)
            )
            self.conn.commit()
        except Exception as e:
            _LOGGER.error("DB log_temperature error: %s", e)

    def log_control_action(self, htemp: float, target: float, mode: str, fan: str):
        try:
            self.conn.execute(
                "INSERT INTO control_log (htemp_c, target_c, mode, fan_speed) VALUES (?, ?, ?, ?)",
                (htemp, target, mode, fan)
            )
            self.conn.commit()
        except Exception as e:
            _LOGGER.error("DB log_control_action error: %s", e)
