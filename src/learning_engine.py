"""Learning Engine — adjusts target temp based on time-of-day profiles and history."""

import logging
from datetime import datetime, time
from typing import Optional


class LearningEngine:
    def __init__(self, config: dict, db):
        self.cfg = config
        self.db = db
        self.logger = logging.getLogger("LearningEngine")
        self._base_target = float(config["comfort"]["target_temp"])

    def _parse_time(self, t: str) -> time:
        h, m = t.split(":")
        return time(int(h), int(m))

    def _get_time_slot_offset(self) -> float:
        """Return temperature offset based on current time-of-day slot."""
        if not self.cfg["learning"]["enabled"]:
            return 0.0

        now = datetime.now().time()
        slots = self.cfg["learning"]["time_slots"]

        for slot_name, slot in slots.items():
            start = self._parse_time(slot["start"])
            end   = self._parse_time(slot["end"])
            # Handle overnight slots (e.g. 22:00 - 06:00)
            if start > end:
                if now >= start or now < end:
                    return float(slot["offset"])
            else:
                if start <= now < end:
                    return float(slot["offset"])

        return 0.0

    def get_current_target(self) -> float:
        """Return the effective target temperature for the current moment."""
        offset = self._get_time_slot_offset()
        target = self._base_target + offset

        # Clamp to absolute bounds
        min_t = self.cfg["comfort"]["min_temp"]
        max_t = self.cfg["comfort"]["max_temp"]
        target = max(min_t, min(max_t, target))

        self.logger.debug(f"Target temp: {self._base_target}°F + offset {offset:+.1f} = {target:.1f}°F")
        return target
