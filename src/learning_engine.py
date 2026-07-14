"""Learning Engine — time-of-day based target temperature with configurable offsets."""
from __future__ import annotations

import logging
from datetime import datetime, time

_LOGGER = logging.getLogger("LearningEngine")


class LearningEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._base = float(cfg["comfort"]["target_temp_c"])
        self._min = float(cfg["comfort"]["min_temp_c"])
        self._max = float(cfg["comfort"]["max_temp_c"])

    def _slot_offset(self) -> float:
        if not self.cfg["learning"]["enabled"]:
            return 0.0
        now = datetime.now().time()
        for name, slot in self.cfg["learning"]["time_slots"].items():
            start = _parse_t(slot["start"])
            end   = _parse_t(slot["end"])
            in_slot = (now >= start or now < end) if start > end else (start <= now < end)
            if in_slot:
                _LOGGER.debug("Time slot: %s (offset %+.1f)", name, slot["offset"])
                return float(slot["offset"])
        return 0.0

    def get_current_target(self) -> float:
        target = self._base + self._slot_offset()
        return max(self._min, min(self._max, target))


def _parse_t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))
