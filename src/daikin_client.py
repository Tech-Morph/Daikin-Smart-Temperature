"""
Thin wrapper around the Daikin Comfort Control cloud API.

This replicates the auth + request logic from:
  Tech-Morph/daikin_comfort_control/custom_components/daikin_comfort_control/daikin_api.py

Kept as a standalone module so smart-learning-house runs without requiring
the full HA custom component to be installed.

API confirmed via mitmproxy capture of the official Android app (okhttp/4.9.2).
  Base URL:  https://scr.daikincloud.net
  Auth:      POST /common/login  (form-encoded, responds JSON)
  Sensor:    GET  /aircon/get_sensor_info?port=30050&id=&spw=
  Control:   GET  /aircon/get_control_info?port=30050&apw=&id=<username>&spw=
  Set:       GET  /aircon/set_control_info?port=30050&pow=1&mode=X&stemp=X...
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger("DaikinClient")

BASE_URL   = "https://scr.daikincloud.net"
USER_AGENT = "okhttp/4.9.2"
TIMEOUT    = 15


def _parse_kv(text: str) -> dict[str, str]:
    """Parse Daikin plain-text KV responses: 'ret=OK,pow=1,mode=3,...'"""
    result: dict[str, str] = {}
    for pair in text.split(","):
        if "=" in pair:
            k, _, v = pair.strip().partition("=")
            result[k.strip()] = v.strip()
    return result


class DaikinClient:
    def __init__(self, username: str, password: str, uid: str, session: aiohttp.ClientSession):
        self._username = username
        self._password = password
        self._uid = uid
        self._session = session
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self.device_id: str = ""
        self._port: str = "30050"

    async def connect(self) -> None:
        """Authenticate and discover device ID."""
        await self._authenticate()
        await self._discover_device()

    async def _authenticate(self) -> None:
        payload = aiohttp.FormData(quote_fields=False)
        payload.add_field("grant_type", "password")
        payload.add_field("scope",      "smart_app")
        payload.add_field("username",   self._username)
        payload.add_field("password",   self._password)
        raw = await self._request("POST", "/common/login", data=payload, auth=False)
        parsed = _parse_kv(raw) if isinstance(raw, str) else (raw or {})
        self._token = parsed.get("access_token") or parsed.get("accessToken")
        if not self._token:
            raise RuntimeError(f"Login failed — no token in response: {raw}")
        try:
            expires_in = int(parsed.get("expires_in", 600))
        except (ValueError, TypeError):
            expires_in = 600
        self._token_expires_at = time.monotonic() + expires_in - 30
        _LOGGER.info("Daikin auth OK (token valid %ds)", expires_in)

    async def _ensure_auth(self) -> None:
        if not self._token or time.monotonic() >= self._token_expires_at:
            async with self._lock:
                if not self._token or time.monotonic() >= self._token_expires_at:
                    await self._authenticate()

    async def _discover_device(self) -> None:
        raw = await self._request("GET", "/common/device_list")
        parsed = _parse_kv(raw) if isinstance(raw, str) else (raw or {})
        self.device_id = parsed.get("id") or parsed.get("deviceId") or ""
        self._port = str(parsed.get("port", "30050"))
        _LOGGER.info("Device discovered: id=%s port=%s", self.device_id, self._port)

    async def get_state(self) -> dict[str, Any]:
        """Fetch merged sensor + control state. Returns floats/ints where numeric."""
        ctrl_raw = await self._request(
            "GET", "/aircon/get_control_info",
            params={"port": self._port, "apw": "", "id": self._username, "spw": ""},
        )
        sensor_raw = await self._request(
            "GET", "/aircon/get_sensor_info",
            params={"port": self._port, "id": "", "spw": ""},
        )
        ctrl   = _parse_kv(ctrl_raw)   if isinstance(ctrl_raw, str)   else (ctrl_raw or {})
        sensor = _parse_kv(sensor_raw) if isinstance(sensor_raw, str) else (sensor_raw or {})
        merged = {**sensor, **ctrl}  # ctrl takes precedence

        if merged.get("ret") != "OK":
            raise RuntimeError(f"Bad state response: ctrl={ctrl_raw!r:.80} sensor={sensor_raw!r:.80}")

        def _f(k: str, default: float = 0.0) -> float:
            try: return float(merged.get(k, default))
            except (ValueError, TypeError): return default

        return {
            "htemp":  _f("htemp"),   # Indoor temp °C from AC sensor
            "otemp":  _f("otemp"),   # Outdoor temp °C
            "hhum":   _f("hhum"),    # Indoor humidity %
            "stemp":  _f("stemp"),   # Current setpoint °C
            "mode":   merged.get("mode", "1"),
            "f_rate": merged.get("f_rate", "A"),
            "pow":    merged.get("pow", "0"),
            "cmpfreq": _f("cmpfreq"),
        }

    async def set_control(
        self,
        mode: str,
        fan: str,
        stemp: float,
        power: str = "1",
        shum: str = "0",
        f_dir_ud: str = "0",
        f_dir_lr: str = "0",
    ) -> None:
        """
        Issue a set_control_info command.
        mode: Daikin mode code string ("3"=cool, "4"=heat, "6"=fan, "1"=auto)
        fan:  f_rate value ("A", "3", "4", "5", "6", "B")
        stemp: target temp in Celsius
        NOTE: dt3 must mirror stemp for mode=3. dh3=0 required.
        """
        params: dict[str, Any] = {
            "port":     self._port,
            "pow":      power,
            "mode":     mode,
            "stemp":    str(round(stemp, 1)),
            "dt3":      str(round(stemp, 1)),  # Mirror of stemp required for mode=3
            "f_rate":   fan,
            "shum":     shum,
            "dh3":      "0",
            "f_dir_ud": f_dir_ud,
            "f_dir_lr": f_dir_lr,
        }
        raw = await self._request("GET", "/aircon/set_control_info", params=params)
        result = _parse_kv(raw) if isinstance(raw, str) else (raw or {})
        if result.get("ret") != "OK":
            raise RuntimeError(f"set_control_info failed: {raw!r:.200}")
        _LOGGER.debug("set_control_info OK: mode=%s fan=%s stemp=%.1f", mode, fan, stemp)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: aiohttp.FormData | None = None,
        params: dict | None = None,
        auth: bool = True,
        _retry: bool = True,
    ) -> Any:
        if auth:
            await self._ensure_auth()
        headers: dict[str, str] = {
            "x-daikin-uid": self._uid,
            "user-agent":   USER_AGENT,
            "accept-encoding": "gzip",
        }
        if auth and self._token:
            headers["authentication"] = f"bearer {self._token}"
        try:
            async with asyncio.timeout(TIMEOUT):
                async with self._session.request(
                    method, BASE_URL + path, data=data, params=params, headers=headers,
                ) as resp:
                    if resp.status == 401 and auth and _retry:
                        async with self._lock:
                            self._token = None
                            self._token_expires_at = 0.0
                        return await self._request(method, path, data=data, params=params, auth=True, _retry=False)
                    if resp.status == 401:
                        raise RuntimeError("Daikin auth failed (401)")
                    if not resp.ok:
                        text = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                    ct = resp.content_type or ""
                    return await resp.json() if "json" in ct else await resp.text()
        except asyncio.TimeoutError:
            raise RuntimeError(f"Timeout: {method} {path}")
