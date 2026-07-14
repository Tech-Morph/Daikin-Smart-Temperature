"""SQLite persistence layer for sensor readings and control actions."""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path


class SensorDB:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.logger = logging.getLogger("SensorDB")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS temperature_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT NOT NULL DEFAULT (datetime('now')),
                room      TEXT NOT NULL,
                temp_f    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS control_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL DEFAULT (datetime('now')),
                avg_temp    REAL,
                target_temp REAL,
                mode        TEXT,
                fan_speed   TEXT
            );
        """)
        self.conn.commit()

    def log_temperature(self, room: str, temp_f: float):
        try:
            self.conn.execute(
                "INSERT INTO temperature_log (room, temp_f) VALUES (?, ?)",
                (room, temp_f)
            )
            self.conn.commit()
        except Exception as e:
            self.logger.error(f"DB log_temperature error: {e}")

    def log_control_action(self, avg_temp: float, target: float, mode: str, fan: str):
        try:
            self.conn.execute(
                "INSERT INTO control_log (avg_temp, target_temp, mode, fan_speed) VALUES (?, ?, ?, ?)",
                (avg_temp, target, mode, fan)
            )
            self.conn.commit()
        except Exception as e:
            self.logger.error(f"DB log_control_action error: {e}")

    def get_recent_temps(self, room: str, hours: int = 24) -> list[tuple]:
        cur = self.conn.execute(
            "SELECT ts, temp_f FROM temperature_log "
            "WHERE room = ? AND ts >= datetime('now', ?) ORDER BY ts",
            (room, f"-{hours} hours")
        )
        return cur.fetchall()
