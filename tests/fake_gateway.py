"""A fake Lunatone DALI WLAN gateway speaking the websocket protocol.

The message sequences follow a capture of the Lunatone Windows software
talking to firmware 1.1.2/1.0.10:

* daliFrame          -> daliFrame ack {"result": 0}, daliMonitor echo
* daliFrame (twice)  -> two acks, two echoes
* daliFrame (answer) -> ack, echo, daliAnswer {"result": 8, "daliData": x}
                        or {"result": 0} without answer
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web

INFO = {
    "type": "info",
    "data": {
        "name": "Lunatone Dali WLAN",
        "version": "1.1.2/1.0.10",
        "descriptor": {
            "lines": 1,
            "bufferSize": 255,
            "tickResolution": 2000,
            "maxYnFrameSize": 251,
            "deviceListSpecifier": True,
            "protocolVersion": "1.0",
        },
        "device": {
            "serial": 123456789012,
            "gtin": 9010342000000,
            "pcb": "5.a",
            "articleNumber": 24138478,
            "articleInfo": "-PS",
            "productionYear": 2023,
            "productionWeek": 48,
        },
    },
    "timeSignature": {"timestamp": 0.0, "counter": 0},
}


@dataclass
class FakeInstance:
    """An input device instance."""

    instance_type: int = 1
    enabled: bool = True
    event_scheme: int = 2
    resolution: int = 1
    value: int = 0

    def value_bytes(self) -> list[int]:
        """Return the input value MSB aligned, as QUERY INPUT VALUE (LATCH) do."""
        count = max(1, (self.resolution + 7) // 8)
        raw = self.value << (count * 8 - self.resolution)
        return list(raw.to_bytes(count, "big"))


def cs_mini(lux: int = 40, occupancy: int = 0) -> list[FakeInstance]:
    """Return the instances of a Lunatone DALI-2 CS Mini."""
    return [
        FakeInstance(instance_type=3, resolution=2, value=occupancy),
        FakeInstance(instance_type=4, resolution=11, value=lux),
    ]


@dataclass
class FakeGateway:
    """Websocket server that behaves like the gateway."""

    devices: dict[int, list[FakeInstance]] = field(default_factory=dict)
    received: list[dict[str, Any]] = field(default_factory=list)
    clients: list[web.WebSocketResponse] = field(default_factory=list)
    ack_result: int = 0
    respond: bool = True
    answer_pings: bool = True
    pongs: list[bytes] = field(default_factory=list)
    host: str = ""
    _runner: web.AppRunner | None = None
    _latch: list[int] = field(default_factory=list)
    _counter: int = 0

    @property
    def frames(self) -> list[list[int]]:
        """Return the DALI frames clients sent."""
        return [
            message["data"]["daliData"]
            for message in self.received
            if message.get("type") == "daliFrame"
        ]

    async def start(self) -> None:
        """Listen on a free localhost port."""
        app = web.Application()
        app.router.add_get("/", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.host = f"127.0.0.1:{port}"

    async def stop(self) -> None:
        """Close all connections and the server."""
        await self.disconnect_clients()
        if self._runner is not None:
            await self._runner.cleanup()

    async def disconnect_clients(self) -> None:
        """Drop all client connections."""
        for ws in list(self.clients):
            await ws.close()

    async def ping_clients(self, data: bytes) -> None:
        """Ping all clients like the gateway does every 5 seconds."""
        for ws in list(self.clients):
            await ws.ping(data)

    async def push_monitor(self, data: list[int]) -> None:
        """Send a frame seen on the bus to all clients."""
        for ws in list(self.clients):
            await self._send_monitor(ws, data)

    async def _send_monitor(self, ws: web.WebSocketResponse, data: list[int]) -> None:
        self._counter += 1
        await ws.send_json(
            {
                "type": "daliMonitor",
                "data": {
                    "tick_us": 27382,
                    "timestamp": 4472162946096 + self._counter,
                    "bits": len(data) * 8,
                    "data": data,
                    "line": 0,
                },
                "timeSignature": {
                    "timestamp": 4472162946096 + self._counter,
                    "counter": self._counter,
                },
            }
        )

    def answer(self, data: list[int]) -> int | None:
        """Return the backward frame for a query, None for no answer."""
        if len(data) != 3 or not data[0] & 1 or data[0] & 0x80:
            return None
        address = data[0] >> 1
        if (instances := self.devices.get(address)) is None:
            return None
        if data[1] == 0xFE:
            return len(instances) if data[2] == 0x35 else None
        if data[1] >= len(instances):
            return None
        instance = instances[data[1]]
        if data[2] == 0x8C:
            first, *self._latch = instance.value_bytes()
            return first
        if data[2] == 0x8D:
            return self._latch.pop(0) if self._latch else None
        return {
            0x80: instance.instance_type,
            0x81: instance.resolution,
            0x86: 0xFF if instance.enabled else None,
            0x8B: instance.event_scheme,
        }.get(data[2])

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(autoping=self.answer_pings)
        await ws.prepare(request)
        self.clients.append(ws)
        await ws.send_json(INFO)
        try:
            async for message in ws:
                if message.type is WSMsgType.PONG:
                    self.pongs.append(message.data)
                if message.type is not WSMsgType.TEXT:
                    continue
                payload = message.json()
                self.received.append(payload)
                if payload.get("type") == "daliFrame" and self.respond:
                    await self._handle_frame(ws, payload["data"])
        finally:
            self.clients.remove(ws)
        return ws

    async def _handle_frame(self, ws: web.WebSocketResponse, data: dict) -> None:
        frame = data["daliData"]
        mode = data["mode"]
        for _ in range(2 if mode["sendTwice"] else 1):
            await ws.send_json(
                {"type": "daliFrame", "data": {"line": 0, "result": self.ack_result}}
            )
            if self.ack_result:
                return
            await self._send_monitor(ws, frame)
            await asyncio.sleep(0)
        if mode["waitForAnswer"]:
            answer = self.answer(frame)
            if answer is None:
                reply: dict[str, Any] = {"line": 0, "result": 0}
            else:
                reply = {"line": 0, "result": 8, "daliData": answer}
            await ws.send_json({"type": "daliAnswer", "data": reply})
