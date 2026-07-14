"""Daikin LAN API controller using pydaikin."""

import logging
from pydaikin.daikin_base import Appliance

MODE_MAP = {
    "cool": "3",
    "heat": "4",
    "fan":  "6",
    "auto": "1",
    "dry":  "2",
}

class DaikinController:
    def __init__(self, ip: str, port: int = 80):
        self.ip = ip
        self.port = port
        self.logger = logging.getLogger("DaikinController")
        self._device = None

    async def _get_device(self):
        if self._device is None:
            self._device = await Appliance.factory(self.ip)
        return self._device

    async def set_control(self, mode: str, fan: str, setpoint: float):
        """
        mode: 'cool' | 'heat' | 'fan' | 'auto' | 'dry'
        fan:  '1'=auto '2'=low '3'=medium '4'=high '5'=night
        setpoint: target temperature in Fahrenheit (converted to Celsius for Daikin)
        """
        device = await self._get_device()

        # Daikin API uses Celsius
        setpoint_c = (setpoint - 32) * 5 / 9
        mode_code = MODE_MAP.get(mode, "1")

        params = {
            "pow": "1",              # Power ON
            "mode": mode_code,
            "stemp": str(round(setpoint_c, 1)),
            "f_rate": fan,
            "f_dir": "0",           # Swing off — change if desired
        }

        self.logger.info(f"Sending to Daikin: mode={mode}({mode_code}) fan={fan} setpoint={setpoint:.1f}°F ({setpoint_c:.1f}°C)")
        await device.set(params)

    async def get_status(self) -> dict:
        device = await self._get_device()
        await device.update_status()
        return {
            "power": device.values.get("pow"),
            "mode":  device.values.get("mode"),
            "temp":  device.values.get("htemp"),  # Current indoor temp from unit
            "setpoint": device.values.get("stemp"),
            "fan":   device.values.get("f_rate"),
        }
