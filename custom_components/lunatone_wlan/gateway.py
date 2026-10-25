"""Websocket client for the Lunatone DALI WLAN gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10
REQUEST_TIMEOUT = 5
HEARTBEAT = 30
RECONNECT_DELAYS = (1, 2, 5, 10, 30)

# daliAnswer result when an 8-bit backward frame was received, 0 means no answer
RESULT_ANSWER = 8


class GatewayError(Exception):
    """The gateway did not handle a request."""


class GatewayConnectionError(GatewayError):
    """The gateway cannot be reached."""


@dataclass(frozen=True, slots=True)
class MonitorFrame:
    """A frame seen on the DALI bus."""

    bits: int
    data: tuple[int, ...]
    line: int


@dataclass(slots=True)
class _Request:
    acks_expected: int
    wait_for_answer: bool
    future: asyncio.Future[int | None]
    acks: int = 0


class LunatoneGateway:
    """Connection to a gateway at ws://<host>/.

    Frames are sent one at a time: the gateway acks every transmission (twice
    for send twice frames) and follows up with a daliAnswer when an answer was
    requested.
    """

    def __init__(self, session: aiohttp.ClientSession, host: str) -> None:
        """Initialize the client."""
        self._session = session
        self.host = host
        self.info: dict[str, Any] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._lock = asyncio.Lock()
        self._request: _Request | None = None
        self._monitor_listeners: list[Callable[[MonitorFrame], None]] = []
        self._connection_listeners: list[Callable[[bool], None]] = []
        self._closing = False

    @property
    def connected(self) -> bool:
        """Return True while the websocket is open."""
        return self._ws is not None and not self._ws.closed

    @property
    def serial(self) -> str | None:
        """Return the gateway serial number."""
        serial = self.info.get("device", {}).get("serial")
        return None if serial is None else str(serial)

    async def connect(self) -> None:
        """Open the websocket and wait for the gateway's info message."""
        ws: aiohttp.ClientWebSocketResponse | None = None
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                ws = await self._session.ws_connect(
                    f"ws://{self.host}/", heartbeat=HEARTBEAT
                )
                while True:
                    message = await ws.receive_json()
                    if message.get("type") == "info":
                        self.info = message.get("data") or {}
                        break
                await ws.send_json(
                    {"type": "filtering", "data": {"ping": True, "group": True}}
                )
        except (aiohttp.ClientError, TimeoutError, TypeError, ValueError) as err:
            if ws is not None:
                await ws.close()
            raise GatewayConnectionError(
                f"Cannot connect to gateway at {self.host}: {err!r}"
            ) from err
        if self._closing:
            await ws.close()
            raise GatewayConnectionError("Connection closed while connecting")
        self._ws = ws

    async def run(self) -> None:
        """Read messages and reconnect until close() is called."""
        failures = 0
        while not self._closing:
            if not self.connected:
                try:
                    await self.connect()
                except GatewayConnectionError as err:
                    if self._closing:
                        break
                    delay = RECONNECT_DELAYS[min(failures, len(RECONNECT_DELAYS) - 1)]
                    failures += 1
                    _LOGGER.debug("%s, retrying in %ss", err, delay)
                    await asyncio.sleep(delay)
                    continue
                _LOGGER.info("Reconnected to gateway at %s", self.host)
            failures = 0
            self._notify_connection(True)
            await self._read_loop()
            self._notify_connection(False)
            if not self._closing:
                _LOGGER.warning("Lost connection to gateway at %s", self.host)

    async def close(self) -> None:
        """Close the connection and stop reconnecting."""
        self._closing = True
        if self._ws is not None:
            await self._ws.close()

    def add_monitor_listener(
        self, listener: Callable[[MonitorFrame], None]
    ) -> Callable[[], None]:
        """Call listener for every frame seen on the bus."""
        self._monitor_listeners.append(listener)
        return lambda: self._monitor_listeners.remove(listener)

    def add_connection_listener(
        self, listener: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Call listener when the connection opens or closes."""
        self._connection_listeners.append(listener)
        return lambda: self._connection_listeners.remove(listener)

    async def send_frame(
        self,
        data: Sequence[int],
        *,
        send_twice: bool = False,
        wait_for_answer: bool = False,
        priority: int = 4,
    ) -> int | None:
        """Send a 16 or 24-bit forward frame and return the answer, if any."""
        if len(data) not in (2, 3) or not all(0 <= byte <= 0xFF for byte in data):
            raise ValueError(f"Invalid DALI frame {list(data)}")
        async with self._lock:
            ws = self._ws
            if ws is None or ws.closed:
                raise GatewayConnectionError(f"Not connected to {self.host}")
            future: asyncio.Future[int | None] = (
                asyncio.get_running_loop().create_future()
            )
            self._request = _Request(2 if send_twice else 1, wait_for_answer, future)
            try:
                await ws.send_json(
                    {
                        "data": {
                            "daliData": list(data),
                            "line": 0,
                            "mode": {
                                "priority": priority,
                                "sendTwice": send_twice,
                                "waitForAnswer": wait_for_answer,
                            },
                            "numberOfBits": len(data) * 8,
                        },
                        "type": "daliFrame",
                    }
                )
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    return await future
            except TimeoutError as err:
                raise GatewayError(f"Gateway did not confirm frame {data}") from err
            except (aiohttp.ClientError, ConnectionResetError) as err:
                raise GatewayConnectionError(f"Sending to {self.host} failed") from err
            finally:
                self._request = None

    async def _read_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            async for message in ws:
                if message.type is aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except ValueError:
                        _LOGGER.debug("Ignoring invalid message %s", message.data)
                        continue
                    self._handle_message(payload)
                elif message.type is aiohttp.WSMsgType.ERROR:
                    _LOGGER.debug("Websocket error: %s", ws.exception())
                    break
        finally:
            await ws.close()
            self._ws = None
            if self._request is not None and not self._request.future.done():
                self._request.future.set_exception(
                    GatewayConnectionError(f"Connection to {self.host} lost")
                )

    def _handle_message(self, payload: dict[str, Any]) -> None:
        message_type = payload.get("type")
        data = payload.get("data") or {}
        if message_type == "daliMonitor":
            frame = MonitorFrame(
                bits=data.get("bits", 0),
                data=tuple(data.get("data", ())),
                line=data.get("line", 0),
            )
            for listener in list(self._monitor_listeners):
                try:
                    listener(frame)
                except Exception:
                    _LOGGER.exception("Error handling bus frame %s", frame)
        elif message_type == "daliFrame":
            self._handle_ack(data)
        elif message_type == "daliAnswer":
            self._handle_answer(data)
        elif message_type == "info":
            self.info = data

    def _handle_ack(self, data: dict[str, Any]) -> None:
        request = self._request
        if request is None or request.future.done():
            return
        if (result := data.get("result")) != 0:
            request.future.set_exception(
                GatewayError(f"Gateway rejected frame (result {result})")
            )
            return
        request.acks += 1
        if request.acks >= request.acks_expected and not request.wait_for_answer:
            request.future.set_result(None)

    def _handle_answer(self, data: dict[str, Any]) -> None:
        request = self._request
        if request is None or request.future.done() or not request.wait_for_answer:
            return
        if data.get("result") == RESULT_ANSWER:
            request.future.set_result(data.get("daliData"))
        else:
            request.future.set_result(None)

    def _notify_connection(self, connected: bool) -> None:
        for listener in list(self._connection_listeners):
            listener(connected)
