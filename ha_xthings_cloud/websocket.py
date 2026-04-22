"""WebSocket client for receiving real-time device status from Xthings Cloud."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import aiohttp

from .const import WS_URL

_LOGGER = logging.getLogger(__name__)

RECONNECT_MIN_DELAY = 5
RECONNECT_MAX_DELAY = 300


class XthingsCloudWebSocket:
    """Xthings Cloud WebSocket client with auto-reconnect.

    Args:
        session: aiohttp.ClientSession instance (injected).
        token: Auth token for WebSocket login.
        on_device_status: Callback(device_uuid, status_dict) for device updates.
        on_token_expired: Async callback when auth error is received.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        on_device_status: Callable[[str, dict[str, Any]], None],
        on_token_expired: Callable[[], Any],
    ) -> None:
        self._session = session
        self._token = token
        self._on_device_status = on_device_status
        self._on_token_expired = on_token_expired
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._reconnect_delay = RECONNECT_MIN_DELAY
        self._stopping = False

    @property
    def token(self) -> str:
        """Return current auth token."""
        return self._token

    @token.setter
    def token(self, value: str) -> None:
        """Update auth token (e.g. after refresh)."""
        self._token = value

    async def async_start(self) -> None:
        """Start WebSocket connection loop."""
        self._stopping = False
        self._task = asyncio.create_task(self._async_run())

    async def async_stop(self) -> None:
        """Stop WebSocket connection and cleanup."""
        self._stopping = True
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _async_run(self) -> None:
        """Main loop with exponential backoff reconnect."""
        while not self._stopping:
            try:
                await self._async_connect()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                _LOGGER.exception("WebSocket unexpected error")
            if self._stopping:
                break
            _LOGGER.info("WebSocket reconnecting in %s seconds", self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX_DELAY)

    async def _async_connect(self) -> None:
        """Establish connection, authenticate, and process messages."""
        _LOGGER.info("WebSocket connecting to %s", WS_URL)
        try:
            self._ws = await self._session.ws_connect(WS_URL, heartbeat=30)
        except Exception as err:
            _LOGGER.error("WebSocket connection failed: %s (%s)", err, type(err).__name__)
            return

        _LOGGER.info("WebSocket connected")
        self._reconnect_delay = RECONNECT_MIN_DELAY

        # Authenticate
        await self._ws.send_json({"cmd": "login", "data": {"x-token": self._token}})
        _LOGGER.info("WebSocket login sent")
        self._ping_task = asyncio.create_task(self._async_ping_loop())

        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    _LOGGER.debug("WebSocket raw message: %s", msg.data)
                    await self._async_handle_message(msg.json())
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.error("WebSocket error: %s", self._ws.exception())
                    break
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    _LOGGER.debug("WebSocket closed by server")
                    break
        except aiohttp.ClientError as err:
            _LOGGER.error("WebSocket read error: %s", err)
        finally:
            self._stop_ping()
            if self._ws and not self._ws.closed:
                await self._ws.close()

    def _stop_ping(self) -> None:
        """Cancel ping task."""
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        self._ping_task = None

    async def _async_ping_loop(self) -> None:
        """Send application-level ping every 55 seconds."""
        try:
            while self._ws and not self._ws.closed:
                await asyncio.sleep(55)
                if self._ws and not self._ws.closed:
                    await self._ws.send_json({"cmd": "ping"})
                    _LOGGER.debug("WebSocket ping sent")
        except asyncio.CancelledError:
            pass
        except aiohttp.ClientError as err:
            _LOGGER.error("WebSocket ping failed: %s", err)

    async def _async_handle_message(self, message: dict[str, Any]) -> None:
        """Handle incoming WebSocket message and dispatch to callbacks."""
        cmd = message.get("cmd")
        data = message.get("data", [])
        _LOGGER.debug("WebSocket message: cmd=%s", cmd)

        if cmd == "report.device.status":
            self._handle_device_status(data)

        elif cmd == "report.device.photo":
            self._handle_device_photo(data)

        elif cmd == "report.device.connected":
            self._handle_device_connected(data)

        elif cmd == "auth_error":
            _LOGGER.warning("WebSocket auth error, triggering token refresh")
            await self._on_token_expired()

        elif cmd == "pong":
            _LOGGER.debug("WebSocket pong received")

        else:
            _LOGGER.debug("WebSocket unknown cmd: %s", cmd)

    def _handle_device_status(self, data: Any) -> None:
        """Process device status report with field conversions."""
        if not isinstance(data, list):
            _LOGGER.warning("report.device.status data is not a list")
            return
        for device in data:
            device_uuid = device.get("uuid")
            status = device.get("status")
            if not device_uuid or not status:
                continue
            # Field conversions
            if "power" in status:
                status["on"] = status["power"] == 1
            if "is_locked" in status:
                is_locked = status["is_locked"]
                status["locked"] = is_locked == 2
                status["jammed"] = is_locked == 3
            self._on_device_status(device_uuid, status)

    def _handle_device_photo(self, data: Any) -> None:
        """Process device photo/snapshot report."""
        if not isinstance(data, list):
            return
        for device in data:
            device_uuid = device.get("uuid")
            photo = device.get("status", {}).get("photo")
            if device_uuid and photo:
                self._on_device_status(device_uuid, {"snapshot_url": photo})

    def _handle_device_connected(self, data: Any) -> None:
        """Process device online/offline report."""
        if not isinstance(data, list):
            return
        for device in data:
            device_uuid = device.get("uuid")
            online = device.get("status", {}).get("online")
            if device_uuid and online is not None:
                self._on_device_status(device_uuid, {"online": online})
